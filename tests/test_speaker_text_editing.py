"""Name/merge/text edits save together, preserve timing/format, reject conflicts."""
import codecs
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.store import NoteStore
from src.summaries import Summaries
from src.text_library import TextLibrary
from src.text_files import write_text


class SpeakerTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.folder=self.root/'Downloads';self.folder.mkdir()
        self.store=NoteStore(self.root/'data/notes.sqlite3');self.library=TextLibrary(self.root/'library.sqlite3')
        for name,value in dict(STORE=self.store,TEXT_LIBRARY=self.library,SUMMARIES=Summaries(self.root/'summaries')).items():
            self.enterContext(patch.object(main,name,value))
        self.enterContext(patch('src.job_artifacts.DEFAULT_DOWNLOADS',self.folder))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def save(self,note,text,**changes):
        return await self.client.post(f"/api/notes/{note['id']}/speakers",json={
            'speaker_names':note['speaker_names'],'text_hash':note['text_hash'],
            'segments':[{'id':row['id'],'text':text if index==0 else row['text'],
                         'start':9999,'speaker':'unknown'} for index,row in enumerate(note['segments'])],**changes})

    def file_note(self,text,extension='txt',encoding='utf-8',prefix=b''):
        path=self.folder/f'fixture.{extension}';path.write_bytes(prefix+text.encode(encoding))
        self.library.scan(str(self.folder));note=self.library.get(self.library.scan(str(self.folder))[0]['id'])
        return path,note

    async def test_txt_names_and_text_save_together_preserving_bom_crlf_and_timestamps(self):
        original='[00:00:01] Alice: wrong\r\n\r\n[00:00:03] Bob: keep\r\n'
        path,note=self.file_note(original,encoding='utf-16-le',prefix=codecs.BOM_UTF16_LE)
        result=await self.save(note,'corrected\nsecond line',speaker_names={'S01':'민수','S02':'지민'})
        self.assertEqual(result.status_code,200,result.text)
        expected='[00:00:01] 민수: corrected\r\nsecond line\r\n\r\n[00:00:03] 지민: keep\r\n'
        self.assertEqual(path.read_bytes(),codecs.BOM_UTF16_LE+expected.encode('utf-16-le'))
        self.assertEqual([s['start'] for s in result.json()['segments']],[1,3])

    async def test_srt_and_vtt_preserve_cue_ids_settings_other_cues_and_voice_wrappers(self):
        for extension in ('srt','vtt'):
            with self.subTest(extension=extension):
                original=('WEBVTT\n\nNOTE untouched\n\n' if extension=='vtt' else '')+'cue-17\n00:00:01.000 --> 00:00:02.000 align:start\n<v Alice><b>wrong</b></v>\n\ncue-18\n00:00:03.000 --> 00:00:04.000\n<v Bob>keep</v>\n'
                path,note=self.file_note(original,extension)
                # Select the specific newly indexed file; the previous subtest
                # deliberately remains in the same folder.
                note=next(self.library.get(n['id']) for n in self.library.scan(str(self.folder)) if n['text_path']==str(path))
                result=await self.save(note,'new & corrected')
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(path.read_text(),original.replace('wrong','new &amp; corrected'))

    async def test_managed_note_merge_and_text_commit_without_changing_raw_or_timing(self):
        identity='a'*32;audio=self.store.upload_directory(identity)/'source.wav'
        self.store.create(dict(id=identity,title='fixture',original_filename='source.wav',source_path=str(audio),status='done'))
        rows=[dict(id=0,start=1,end=2,speaker='S01',text='wrong'),dict(id=1,start=3,end=4,speaker='S02',text='keep')]
        note=self.store.update(identity,segments=rows,raw_segments=rows,speaker_names={'S01':'Alice','S02':'Bob'})
        note=self.store.update(identity,**write_text(note,str(self.folder)))
        result=await self.save(note,'corrected',speaker_names={'S01':'민수','S02':'Bob'},speaker_merges={'S02':'S01'})
        self.assertEqual(result.status_code,200,result.text)
        saved=self.store.get(identity)
        self.assertEqual(saved['raw_segments'],rows)
        self.assertEqual([(r['start'],r['end'],r['speaker']) for r in saved['segments']],[(1,2,'S01'),(3,4,'S01')])
        self.assertIn('민수: corrected',Path(saved['text_path']).read_text())
        stale=await self.save(note,'stale overwrite')
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.store.get(identity)['segments'][0]['text'],'corrected')

    async def test_external_conflict_and_invalid_segments_never_write_partial_changes(self):
        path,note=self.file_note('[00:00:01] Alice: original\n')
        before=path.read_bytes()
        bad=await self.client.post(f"/api/notes/{note['id']}/speakers",json={'speaker_names':{'S01':'changed'},'text_hash':note['text_hash'],'segments':[]})
        self.assertEqual(bad.status_code,409);self.assertEqual(path.read_bytes(),before)
        path.write_text('changed outside app')
        stale=await self.save(note,'overwrite')
        self.assertEqual(stale.status_code,409);self.assertEqual(path.read_text(),'changed outside app')
