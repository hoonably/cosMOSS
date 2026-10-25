"""Live-catalog protocols, selection persistence and per-job CLI options."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx

from src.summary_models import SummaryModels, discover_models, normalize_models
from src.summaries import command
from src.summaries import Summaries


def model(identity='current', *, recommended=True, efforts=None):
    return {'id':identity, 'name':identity.title(), 'recommended':recommended,
            'efforts':efforts if efforts is not None else ['low','medium','high'], 'default_effort':'low'}


class ModelPreferencesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = SummaryModels(self.root)
        self.rows = [model(), model('alternative', recommended=False)]
        self.enterContext(patch('src.summary_models.executable', return_value=sys.executable))
        self.discovery = self.enterContext(patch('src.summary_models.discover_models', new=AsyncMock(side_effect=lambda *_:self.rows)))

    async def test_defaults_are_real_model_and_medium_without_saving_a_fixed_default(self):
        value = await self.manager.get('codex')
        self.assertEqual(value['selection'], {'model':'current','effort':'medium'})
        self.assertFalse(self.manager.preferences_path.exists())
        self.rows = [model('new-default')]
        self.assertEqual((await self.manager.get('codex', refresh=True))['selection']['model'], 'new-default')

    async def test_preferences_survive_restart_and_new_default_and_are_provider_specific(self):
        await self.manager.select('codex','alternative','high')
        await self.manager.select('claude','current','low')
        self.rows = [model('latest'), model('alternative', recommended=False), model('current', recommended=False)]
        manager = SummaryModels(self.root)
        result = await manager.get('codex', refresh=True)
        self.assertEqual(result['selection'], {'model':'alternative','effort':'high'})
        self.assertEqual((await manager.get('claude', refresh=True))['selection'], {'model':'current','effort':'low'})
        self.assertEqual((await SummaryModels(self.root).get('codex'))['selection'],result['selection'])

    async def test_removed_model_resets_to_current_default_medium_and_reports_it(self):
        await self.manager.select('codex','alternative','high')
        self.rows = [model('replacement')]
        result = await self.manager.get('codex', refresh=True)
        self.assertEqual(result['selection'], {'model':'replacement','effort':'medium'})
        self.assertIn('사라져', result['notice'])
        self.assertNotIn('codex',self.manager._read(self.manager.preferences_path))

    async def test_failed_refresh_or_missing_cli_preserves_saved_choice(self):
        await self.manager.select('codex','alternative','high')
        before = self.manager.preferences_path.read_bytes()
        self.discovery.side_effect = TimeoutError()
        result = await self.manager.get('codex', refresh=True)
        self.assertTrue(result['stale'])
        self.assertEqual(result['selection'],{'model':'alternative','effort':'high'})
        with patch('src.summary_models.executable', return_value=None):
            self.assertEqual((await self.manager.get('codex'))['selection'],result['selection'])
        self.assertEqual(before,self.manager.preferences_path.read_bytes())

    async def test_effort_capability_change_keeps_model_and_updates_effort(self):
        await self.manager.select('codex','alternative','high')
        self.rows = [model('alternative', efforts=['medium','new-effort'])]
        result = await self.manager.get('codex', refresh=True)
        self.assertEqual(result['selection'],{'model':'alternative','effort':'medium'})
        self.assertIn('추론 강도',result['notice'])
        with self.assertRaises(ValueError): await self.manager.select('codex','not-in-catalog','medium')
        with self.assertRaises(ValueError): await self.manager.resolve('codex','alternative','high')
        self.assertEqual((await self.manager.resolve('codex','alternative','new-effort'))['effort'],'new-effort')

    async def test_no_effort_model_omits_effort_option(self):
        self.rows = [model(efforts=[])]
        result = await self.manager.get('claude')
        self.assertEqual(result['selection']['effort'],'')
        self.assertEqual((await self.manager.resolve('claude'))['model'],'current')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def binary(self, body):
        binary = self.root / 'fake-cli'
        binary.write_text('#!'+sys.executable+'\nimport json,sys,os,pathlib\n'+body)
        binary.chmod(0o755)
        return str(binary)

    async def test_codex_initialization_and_pagination_never_start_a_thread_or_prompt(self):
        capture = self.root/'requests'
        binary = self.binary(f'''for line in sys.stdin:
 value=json.loads(line)
 with open({str(capture)!r},'a') as out:out.write(line)
 if value['method']=='initialize':result={{}}
 elif value['method']=='initialized':continue
 elif value['method']=='model/list':
  second=value['params'].get('cursor')=='page2'
  result={{'data':[{{'model':'future-2' if second else 'future-1','displayName':'Future Model','isDefault':not second,'supportedReasoningEfforts':[{{'reasoningEffort':'medium'}},{{'reasoningEffort':'future-effort'}}]}}],'nextCursor':None if second else 'page2'}}
 else:raise RuntimeError('Unexpected generation request')
 print(json.dumps({{'id':value['id'],'result':result}}),flush=True)
''')
        rows = await discover_models('codex',binary)
        self.assertEqual([row['id'] for row in rows],['future-1','future-2'])
        self.assertEqual(rows[0]['efforts'],['medium','future-effort'])
        self.assertEqual([json.loads(line)['method'] for line in capture.read_text().splitlines()],['initialize','initialized','model/list','model/list'])

    async def test_claude_sdk_initialization_models_and_capabilities_without_user_message(self):
        capture = self.root/'request'
        binary = self.binary(f'''value=json.loads(sys.stdin.readline())
pathlib.Path({str(capture)!r}).write_text(json.dumps({{'request':value,'args':sys.argv}}))
print(json.dumps({{'type':'control_response','response':{{'subtype':'success','request_id':value['request_id'],'response':{{'models':[{{'value':'opus','resolvedModel':'claude-opus-future','displayName':'Opus Future','supportsEffort':True,'supportedEffortLevels':['low','medium','high']}},{{'value':'haiku','resolvedModel':'claude-haiku-future','displayName':'Haiku Future','supportsEffort':False}}]}}}}}}),flush=True)
for line in sys.stdin:raise RuntimeError('A prompt must not be sent')
''')
        rows = await discover_models('claude',binary)
        self.assertEqual(rows[0]['id'],'claude-opus-future')
        self.assertEqual(rows[1]['efforts'],[])
        captured=json.loads(capture.read_text())
        self.assertEqual(captured['request']['request']['subtype'],'initialize')
        self.assertEqual(captured['args'][captured['args'].index('--tools')+1],'')

    async def test_probe_cancellation_does_not_leave_cli_process(self):
        pidfile=self.root/'pid'
        binary=self.binary(f"import time\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
        task=asyncio.create_task(discover_models('codex',binary))
        for _ in range(50):
            if pidfile.exists():break
            await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)

    def test_cli_options_are_explicit_and_keep_existing_sandbox_controls(self):
        for provider in ('codex','claude'):
            args=command(provider,'/cli',Path('/result'),Path('/schema'),model='future-model',effort='medium')
            self.assertEqual(args[args.index('--model')+1],'future-model')
            if provider=='codex':
                self.assertIn('model_reasoning_effort="medium"',args)
                self.assertEqual(args[args.index('--sandbox')+1],'read-only')
            else:
                self.assertEqual(args[args.index('--effort')+1],'medium')
                self.assertIn('--strict-mcp-config',args)

    def test_claude_default_row_displays_actual_model_and_ignores_duplicates(self):
        rows=normalize_models('claude',[
            {'value':'default','resolvedModel':'claude-current','displayName':'Default','supportsEffort':True,'supportedEffortLevels':['medium']},
            {'value':'opus','resolvedModel':'claude-current','displayName':'Opus Current','supportsEffort':True,'supportedEffortLevels':['medium']},
        ])
        self.assertEqual(rows[0]['name'],'Opus Current')
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['recommended'])


class ModelAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_and_summary_snapshot_reach_actual_cli_arguments(self):
        from src import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'source.txt';source.write_text('[00:00] 민수: 검증용 가상 대화입니다.')
            note={'id':'txt_'+'a'*32,'title':'fixture','text_path':str(source),'status':'done',
                  'source_type':'text_file','text_body':source.read_text(),'text_hash':'fixture'}
            binary=root/'codex';capture=root/'argv.json'
            binary.write_text('#!'+sys.executable+'\nimport json,sys,pathlib\n'+
                f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv))\n'+
                "sys.stdin.read()\npathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'corrected_transcript':'검증용 대화','summary_markdown':'# 검증 요약'}))\n")
            binary.chmod(0o755)
            models=SummaryModels(root)
            summaries=Summaries(root/'summaries')
            with patch.object(main,'SUMMARY_MODELS',models), patch.object(main,'SUMMARIES',summaries), \
                 patch.object(main,'review_note',return_value=note), \
                 patch.object(main,'note_cleanup',return_value=SimpleNamespace(hidden_note_ids=lambda:set())), \
                 patch.object(main,'provider_status',new=AsyncMock(return_value={'ready':True})), \
                 patch('src.summaries.executable',return_value=str(binary)), \
                 patch('src.summary_models.executable',return_value=str(binary)), \
                 patch('src.summary_models.discover_models',new=AsyncMock(return_value=[model()])):
                try:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                        response=await client.put('/api/summary/models/selection',json={'provider':'codex','model':'current','effort':'high'})
                        self.assertEqual(response.status_code,200,response.text)
                        self.assertEqual((await client.get('/api/summary/models?provider=codex&refresh=true')).json()['selection']['effort'],'high')
                        response=await client.post('/api/notes/'+note['id']+'/summary',json={'provider':'codex','model':'current','effort':'high','directory':str(root)})
                        self.assertEqual(response.status_code,202,response.text)
                        task=summaries.tasks.get(note['id'])
                        if task: await task
                        state=summaries.status(note)
                        self.assertEqual(state['phase'],'done',state.get('error'))
                        self.assertEqual(state['result']['model'],'current')
                        self.assertEqual(state['result']['effort'],'high')
                        args=json.loads(capture.read_text())
                        self.assertEqual(args[args.index('--model')+1],'current')
                        self.assertIn('model_reasoning_effort="high"',args)
                        response=await client.put('/api/summary/models/selection',json={'provider':'codex','model':'unknown','effort':'high'})
                        self.assertEqual(response.status_code,409)
                finally:
                    await summaries.close()
