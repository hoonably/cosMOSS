"""Formats round-trip through storage, review, speaker merges and CLI summaries."""
import asyncio
import codecs
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.devices import DeviceStore
from src.store import NoteStore
from src.subtitles import subtitle_fields, rename_subtitle_speakers
from src.summaries import Summaries, markdown_document
from src.text_files import save_text, TextConflict
from src.text_library import TextLibrary
from src.transcript import plain_text, srt_text, vtt_text


SEGMENTS = [{'id':0,'start':1.25,'end':3.5,'speaker':'S01','text':'첫 문장\n이어지는 내용'},
            {'id':1,'start':4,'end':6.125,'speaker':'S02','text':'두 번째 문장'}]
NAMES = {'S01':'지민','S02':'수빈'}


def seed(store, format_name):
    note_id = uuid.uuid4().hex
    folder = store.upload_directory(note_id)
    folder.mkdir(parents=True)
    audio = folder / 'fixture.wav'
    audio.write_bytes(b'RIFF')
    store.create({'id':note_id,'title':'형식 테스트','source_path':str(audio),'original_filename':audio.name,'status':'done','output_format':format_name})
    return store.update(note_id, segments=SEGMENTS, raw_segments=SEGMENTS, speaker_names=NAMES)


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'files'
        self.folder.mkdir()
        self.store = NoteStore(self.root / 'data/moss-note.sqlite3')
        self.library = TextLibrary(self.root / 'library.sqlite3')

    def test_native_formats_save_reopen_edit_and_merge_in_the_selected_format(self):
        for format_name, exporter in [('txt',plain_text),('srt',srt_text),('vtt',vtt_text)]:
            with self.subTest(format=format_name):
                note = seed(self.store,format_name)
                saved = save_text(self.store,note['id'],str(self.folder))
                path = Path(saved['text_path'])
                self.assertEqual(path.suffix,'.'+format_name)
                self.assertEqual(path.read_text(),exporter(SEGMENTS,NAMES))
                self.assertEqual(NoteStore(self.store.database).get(note['id'])['output_format'],format_name)
                saved = save_text(self.store,note['id'],changes={'speaker_names':{'S01':'민수'},'speaker_merges':{'S02':'S01'}})
                self.assertNotIn('수빈',path.read_text())
                self.assertEqual(saved['raw_segments'],SEGMENTS)
                if format_name != 'txt':
                    segments,names = subtitle_fields(path.read_text(),format_name)
                    self.assertEqual(len(names),1)
                    self.assertEqual(segments[1]['end'],6.125)
                    self.assertEqual(segments[0]['text'],SEGMENTS[0]['text'])
                path.write_text('외부 수정')
                with self.assertRaises(TextConflict):
                    save_text(self.store,note['id'],changes={'speaker_names':{'S01':'다른 이름'}})
                self.assertEqual(path.read_text(),'외부 수정')

    def test_scan_srt_vtt_and_txt_without_rewriting_and_keep_original_subtitle_bytes(self):
        for name, text in [('a.SRT',srt_text(SEGMENTS,NAMES)),('b.vtt',vtt_text(SEGMENTS,NAMES)),('c.txt',plain_text(SEGMENTS,NAMES))]:
            (self.folder/name).write_bytes(codecs.BOM_UTF16_LE+text.replace('\n','\r\n').encode('utf-16-le'))
        (self.folder/'ignored.md').write_text('markdown')
        listed = self.library.scan(str(self.folder))
        self.assertEqual({n['output_format'] for n in listed},{'txt','srt','vtt'})
        for metadata in listed:
            note = self.library.get(metadata['id'])
            file = Path(note['text_path'])
            before = file.read_bytes()
            self.assertEqual(len(note['speaker_names']),2)
            self.assertEqual(file.read_bytes(),before)
            saved = self.library.update(note['id'],{'speaker_names':{'S01':'민수'},'speaker_merges':{'S02':'S01'}},note['text_hash'])
            expected = before[len(codecs.BOM_UTF16_LE):].decode('utf-16-le').replace('지민','민수').replace('수빈','민수')
            self.assertEqual(file.read_bytes(),codecs.BOM_UTF16_LE+expected.encode('utf-16-le'))
            self.assertEqual(len(saved['speaker_names']),1)
            self.assertEqual(saved['text_path'],note['text_path'])
        self.assertEqual(self.library.scan(str(self.folder),[n['text_path'] for n in listed]),[])

    def test_vtt_metadata_settings_multiple_voices_and_markup_are_preserved(self):
        text = 'WEBVTT\n\nNOTE keep me\n00:01.000 --> 00:09.000\nnot a cue\n\nSTYLE\n::cue { color: lime; }\n\ncue-id\n00:01.250 --> 00:03.500 align:start position:10%\n<v.one 지민><i>첫 문장</i></v>\n<v 수빈>둘째 &amp; 셋째</v>\n'
        segments,names = subtitle_fields(text,'vtt')
        self.assertEqual(names,{'S01':'지민','S02':'수빈'})
        self.assertEqual(segments[1]['text'],'둘째 & 셋째')
        renamed = rename_subtitle_speakers(text,'vtt',{'지민':'민수','수빈':'민수'})
        self.assertEqual(renamed,text.replace('지민','민수').replace('수빈','민수'))
        self.assertEqual(len(subtitle_fields(renamed,'vtt')[1]),1)

    def test_unlabeled_subtitles_can_be_named_without_changing_times_or_words(self):
        for fmt, text, insertion in [('srt','7\n00:00:01,000 --> 00:00:02,000\n말한 내용\n','민수: '),('vtt','WEBVTT\n\n00:01.000 --> 00:02.000\n말한 내용\n','<v 민수>')]:
            segments,names = subtitle_fields(text,fmt)
            self.assertEqual(names,{'S01':'화자 미지정'})
            self.assertEqual(segments[0]['end'],2)
            self.assertEqual(rename_subtitle_speakers(text,fmt,{'화자 미지정':'화자 미지정'}),text)
            self.assertEqual(rename_subtitle_speakers(text,fmt,{'화자 미지정':'민수'}),text.replace('말한 내용',insertion+'말한 내용'))

    def test_vtt_escaping_round_trips_and_invalid_cues_remain_editable_as_source(self):
        text = vtt_text([{**SEGMENTS[0],'text':'<b> literal & text'}],{'S01':'A & B'})
        segments,names = subtitle_fields(text,'vtt')
        self.assertEqual(names,{'S01':'A & B'})
        self.assertEqual(segments[0]['text'],'<b> literal & text')
        file = self.folder/'broken.srt'
        file.write_text('시간 정보 없는 자막 원문')
        note = self.library.get(self.library.scan(str(self.folder))[0]['id'])
        self.assertEqual(note['segments'],[])
        saved = self.library.update(note['id'],{'text_body':'직접 고친 원문'},note['text_hash'])
        self.assertEqual(saved['text_body'],'직접 고친 원문')

    def test_unvoiced_prefix_in_a_multi_voice_cue_is_not_lost(self):
        text='WEBVTT\n\n00:01.000 --> 00:03.000\n앞부분 <v 지민>화자가 있는 부분\n'
        segments,names=subtitle_fields(text,'vtt')
        self.assertEqual([s['text'] for s in segments],['앞부분','화자가 있는 부분'])
        self.assertEqual(names,{'S01':'화자 미지정','S02':'지민'})

    def test_literal_markup_and_entities_round_trip_in_both_subtitle_formats(self):
        for fmt,exporter in [('srt',srt_text),('vtt',vtt_text)]:
            text=exporter([{**SEGMENTS[0],'text':'<tag> literal & text'}],{'S01':'A & B'})
            segments,names=subtitle_fields(text,fmt)
            self.assertEqual(names,{'S01':'A & B'})
            self.assertEqual(segments[0]['text'],'<tag> literal & text')
            renamed=rename_subtitle_speakers(text,fmt,{'A & B':'C & D'})
            self.assertEqual(subtitle_fields(renamed,fmt)[1],{'S01':'C & D'})


class SubtitleAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root/'files'
        self.folder.mkdir()
        self.store = NoteStore(self.root/'data/moss-note.sqlite3')
        self.library = TextLibrary(self.root/'library.sqlite3')
        self.summaries = Summaries(self.root/'summaries')
        self.devices = DeviceStore(self.root/'devices.sqlite3')
        for name,value in {'STORE':self.store,'TEXT_LIBRARY':self.library,'SUMMARIES':self.summaries,'DEVICES':self.devices,'QUEUE':asyncio.Queue()}.items():
            self.enterContext(patch.object(main,name,value))
        self.addAsyncCleanup(self.summaries.close)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def test_create_uses_device_chunk_setting_and_persists_requested_format(self):
        self.devices.configure('local',options={'chunk_minutes':11})
        for fmt in ('txt','srt','vtt'):
            upload_id = uuid.uuid4().hex
            folder = self.store.upload_directory(upload_id)
            folder.mkdir(parents=True)
            source = folder/'source.wav'
            source.write_bytes(b'RIFF')
            self.store.create_upload({'id':upload_id,'original_filename':'source.wav','source_path':str(source),'status':'ready','duration':1})
            response = await self.client.post('/api/notes',data={'upload_id':upload_id,'device_id':'local','output_format':fmt})
            self.assertEqual(response.status_code,202,response.text)
            note = self.store.get(response.json()['id'])
            self.assertEqual(note['output_format'],fmt)
            self.assertEqual(note['chunk_seconds'],660)
        response = await self.client.post('/api/notes',data={'upload_id':'missing','output_format':'exe'})
        self.assertEqual(response.status_code,422)

    async def test_subtitle_edit_merge_export_and_summary_preserve_source(self):
        for fmt,exporter in [('srt',srt_text),('vtt',vtt_text)]:
            file = self.folder/f'기존 파일.{fmt}'
            file.write_text(exporter(SEGMENTS,NAMES))
            listed = (await self.client.get('/api/text-library',params={'directory':str(self.folder)})).json()
            note_id = next(n['id'] for n in listed if n['text_path']==str(file))
            note = (await self.client.get('/api/notes/'+note_id)).json()
            response = await self.client.post('/api/notes/'+note_id+'/speakers',json={'speaker_names':{'S01':'민수'},'speaker_merges':{'S02':'S01'},'text_hash':note['text_hash']})
            self.assertEqual(response.status_code,200,response.text)
            note = response.json()
            self.assertEqual(len(note['speaker_names']),1)
            before = file.read_bytes()
            response = await self.client.get('/api/notes/'+note_id+'/export/'+fmt)
            self.assertEqual(response.content,before)
            self.assertIn('민수:',(await self.client.get('/api/notes/'+note_id+'/export/txt')).text)
            await self.summarize(note)
            self.assertEqual(file.read_bytes(),before)

    async def test_native_subtitle_summary_is_not_stale_until_the_transcript_changes(self):
        for fmt in ('srt','vtt'):
            note = seed(self.store,fmt)
            saved = save_text(self.store,note['id'],str(self.folder))
            await self.summarize(saved)
            self.assertFalse(self.summaries.status(saved)['stale'])
            segments = [dict(s) for s in saved['segments']]
            segments[0]['text'] = '편집된 내용'
            response = await self.client.patch('/api/notes/'+note['id'],json={'segments':segments})
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(self.summaries.status(self.store.get(note['id']))['stale'])
            self.assertEqual(Path(saved['text_path']).suffix,'.'+fmt)

    async def test_txt_without_end_times_does_not_export_fabricated_subtitle_times(self):
        file = self.folder/'기존 텍스트.txt'
        file.write_text(plain_text(SEGMENTS,NAMES))
        listed = (await self.client.get('/api/text-library',params={'directory':str(self.folder)})).json()
        note_id = listed[0]['id']
        for fmt in ('srt','vtt'):
            response = await self.client.get('/api/notes/'+note_id+'/export/'+fmt)
            self.assertEqual(response.status_code,400)

    async def summarize(self,note):
        binary = self.root/'fake-codex'
        capture = self.root/'stdin.txt'
        binary.write_text('#!'+sys.executable+'\nimport sys,pathlib,json\n'+f'pathlib.Path({str(capture)!r}).write_text(sys.stdin.read())\n'+"pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'corrected_transcript':'보정된 전사문','summary_markdown':'# 요약\\n- 핵심 내용'}))\n")
        binary.chmod(0o755)
        with patch.object(self.main,'provider_status',new=AsyncMock(return_value={'ready':True})),patch('src.summaries.executable',return_value=str(binary)):
            response = await self.client.post('/api/notes/'+note['id']+'/summary',json={'provider':'codex','directory':str(self.folder)})
            self.assertEqual(response.status_code,202,response.text)
            task = self.summaries.tasks.get(note['id'])
            if task: await task
        current = (await self.client.get('/api/notes/'+note['id'])).json()
        result = self.summaries.status(current)
        self.assertEqual(result['phase'],'done',result)
        self.assertFalse(result['stale'])
        self.assertIn('-->',capture.read_text())
        self.assertEqual(Path(result['result']['markdown_path']).read_text(),markdown_document('보정된 전사문','# 요약\n- 핵심 내용'))
        self.assertNotIn('corrected_path', result['result'])
