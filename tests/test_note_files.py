"""Conversions preserve timing; file actions preserve sources and reject stale input."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.note_files import convert_file, read_file
from src.store import NoteStore
from src.summaries import Summaries
from src.text_files import FORMATS, TextConflict, digest, save_text
from src.text_library import TextLibrary


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.segments=[{'id':0,'start':1.125,'end':3.75,'speaker':'S01','text':'첫 내용 & 확인'},
                       {'id':1,'start':4.5,'end':7.25,'speaker':'S02','text':'두 번째 내용'}]
        self.names={'S01':'지민','S02':'수빈'}

    def source(self,format):
        path=self.root/f'회의.{format}'
        path.write_text(FORMATS[format](self.segments,self.names))
        return dict(id='note',status='done',text_path=str(path),text_hash=digest(path.read_bytes()),
                    segments=self.segments,speaker_names=self.names)

    def test_all_six_directions_and_subtitle_txt_round_trip(self):
        for source in FORMATS:
            note=self.source(source);original=Path(note['text_path']).read_bytes()
            for target in set(FORMATS)-{source}:
                converted=convert_file(note,target)
                library=TextLibrary(self.root/'index.sqlite3')
                rows=library.scan(str(self.root))
                output=library.get(next(row['id'] for row in rows if row['text_path']==str(converted)))
                self.assertEqual([s['text'] for s in output['segments']],[s['text'] for s in self.segments])
                self.assertEqual([(s['start'],s['end']) for s in output['segments']],[(s['start'],s['end']) for s in self.segments])
                self.assertEqual(set(output['speaker_names'].values()),set(self.names.values()))
                if target=='txt':
                    back=convert_file(output,'vtt')
                    self.assertIn('00:00:01.125 --> 00:00:03.750',back.read_text())
            self.assertEqual(Path(note['text_path']).read_bytes(),original)

    def test_collisions_symlinks_missing_and_changed_files(self):
        note=self.source('txt')
        outside=self.root/'outside.txt';outside.write_text('keep')
        (self.root/'회의.srt').symlink_to(outside)
        target=convert_file(note,'srt')
        self.assertNotEqual(target,self.root/'회의.srt')
        self.assertEqual(outside.read_text(),'keep')
        self.assertNotEqual(convert_file(note,'srt'),target)
        with self.assertRaises(TextConflict):read_file(note,file_version='stale')
        Path(note['text_path']).write_text('changed')
        with self.assertRaises(TextConflict):convert_file(note,'vtt')
        Path(note['text_path']).unlink()
        with self.assertRaises(ValueError):read_file(note)

    def test_plain_txt_does_not_invent_timing(self):
        path=self.root/'plain.txt';path.write_text('시간 없는 일반 텍스트')
        with self.assertRaisesRegex(ValueError,'시작·종료 시간'):
            convert_file(dict(status='done',source_type='text_file',text_path=str(path)),'srt')
        self.assertFalse(list(self.root.glob('*.srt')))


class FileAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src import main
        self.main=main
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.folder=self.root/'files';self.folder.mkdir()
        self.store=NoteStore(self.root/'data/notes.sqlite3')
        self.library=TextLibrary(self.root/'library.sqlite3')
        self.summaries=Summaries(self.root/'summaries')
        for key,value in {'STORE':self.store,'TEXT_LIBRARY':self.library,'SUMMARIES':self.summaries}.items():
            self.enterContext(patch.object(main,key,value))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.source=self.folder/'회의.srt';self.source.write_text('1\n00:00:01,125 --> 00:00:03,750\n지민: 확인합니다.\n')

    async def files(self):
        return (await self.client.get('/api/text-library',params={'directory':str(self.folder)})).json()

    async def test_conversion_trash_restore_busy_and_origin(self):
        note=(await self.files())[0];base='/api/notes/'+note['id']
        response=await self.client.post(base+'/convert',json={'output_format':'vtt','file_version':note['file_version']})
        self.assertEqual(response.status_code,201,response.text)
        self.assertTrue(self.source.exists())
        self.assertIn('WEBVTT',Path(response.json()['text_path']).read_text())
        for action in ('trash','convert'):
            response=await self.client.post(base+'/'+action,json={'output_format':'vtt'} if action=='convert' else {},headers={'sec-fetch-site':'cross-site'})
            self.assertEqual(response.status_code,403)
        trash=self.root/'Trash';trash.mkdir()
        with patch.object(self.main,'trash_file',side_effect=lambda path:path.rename(trash/path.name)) as move:
            with patch.object(self.summaries,'busy',return_value=True):
                self.assertEqual((await self.client.post(base+'/trash',json={})).status_code,409)
            move.assert_not_called()
            response=await self.client.post(base+'/trash',json={'file_version':'stale'})
            self.assertEqual(response.status_code,409);move.assert_not_called()
            response=await self.client.post(base+'/trash',json={'file_version':note['file_version']})
            self.assertEqual(response.status_code,204,response.text)
        self.assertFalse(self.source.exists());self.assertTrue((trash/self.source.name).exists())
        self.assertFalse(any(n['id']==note['id'] for n in await self.files()))
        (trash/self.source.name).rename(self.source)
        self.assertTrue(any(n['id']==note['id'] for n in await self.files()))

    async def test_reused_path_attaches_only_to_matching_record_and_never_resurrects(self):
        for i in (1,2):
            note_id=str(i)*32
            directory=self.store.upload_directory(note_id);directory.mkdir(parents=True)
            source=directory/'source.wav';source.write_bytes(b'RIFF')
            self.store.create(dict(id=note_id,title='같은 제목',status='done',source_path=str(source),original_filename='source.wav'))
            self.store.update(note_id,text_path=str(self.source),text_hash='old' if i==1 else digest(self.source.read_bytes()))
        metadata=(await self.files())[0]
        self.assertEqual(metadata['managed_note_id'],'2'*32)
        with self.assertRaises(TextConflict):
            read_file(self.store.get('1'*32))
        self.source.unlink()
        self.assertEqual(await self.files(),[])
        with self.assertRaises(TextConflict):save_text(self.store,'2'*32)
        self.assertFalse(self.source.exists())
        self.assertEqual(len(self.store.list()),2)
