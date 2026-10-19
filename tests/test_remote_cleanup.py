"""Server job artifacts are disposable; installed runtimes and models are not."""
import io
import json
import os
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src import remote_bootstrap as bootstrap
from src.remote_workers import RemoteWorkers


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch.object(bootstrap.Path, 'home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {'HF_HOME':str(self.home/'hf')}))
        self.enterContext(patch.object(bootstrap, 'ensure_uv', return_value='/tools/uv'))
        self.enterContext(patch.object(bootstrap, 'inspect_environment', return_value={
            'ready':True,'uv':{'ready':True},'venv':{'ready':True},'packages':{'ready':True}}))
        self.device = {'id':'a'*32,'api_key':'test-key','model':'example/moss','gpu_uuid':'GPU-test','remote_port':18400}
        self.root = self.home/'.local/share/cosmoss/workers'/self.device['id']
        bootstrap.claim_root(self.root,self.device)
        self.python = self.root/'.venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_text('installed interpreter')
        (self.root/'runtime-version').write_text(bootstrap.RUNTIME_VERSION)
        (self.root/'quantization-version').write_text('installed quantization')
        (self.root/'python/bin').mkdir(parents=True)
        (self.root/'python/bin/python3.12').write_text('managed Python')
        self.model = self.home/'hf/hub/models--example--moss/snapshots/main/model.safetensors'
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b'model weights')

    def artifacts(self):
        for name in ('src','tmp','cache','__pycache__'):
            (self.root/name).mkdir(exist_ok=True)
            (self.root/name/'private-recording.wav').write_bytes(b'audio')
        for name in ('worker.log','worker.pid','worker.json'):
            (self.root/name).write_text('transient')

    def test_cleanup_preserves_only_runtime_model_and_unrelated_files(self):
        self.artifacts()
        other = self.root.parent/('b'*32)
        other.mkdir()
        (other/'worker.log').write_text('other device')
        bootstrap.cleanup_job_files(self.root,self.device)
        self.assertEqual({p.name for p in self.root.iterdir()}, {'.venv','python','owner.json','runtime-version','quantization-version'})
        self.assertEqual(self.python.read_text(),'installed interpreter')
        self.assertEqual(self.model.read_bytes(),b'model weights')
        self.assertEqual((other/'worker.log').read_text(),'other device')
        bootstrap.cleanup_job_files(self.root,self.device)  # Idempotent retry.

    def test_cleanup_unlinks_transient_symlinks_without_following_them(self):
        external = self.home/'unrelated'
        external.mkdir()
        (external/'keep.wav').write_bytes(b'keep')
        (self.root/'tmp').symlink_to(external,target_is_directory=True)
        bootstrap.cleanup_job_files(self.root,self.device)
        self.assertEqual((external/'keep.wav').read_bytes(),b'keep')
        self.assertFalse((self.root/'tmp').exists())

    def test_model_location_overlap_blocks_deletion(self):
        self.artifacts()
        self.device['model_source']='folder'
        self.device['model_path']=str(self.root/'tmp')
        with self.assertRaisesRegex(RuntimeError,'모델 경로'):
            bootstrap.cleanup_job_files(self.root,self.device)
        self.assertTrue((self.root/'tmp/private-recording.wav').exists())
        self.assertTrue((self.root/'worker.log').exists())

    def test_wrong_owner_and_symlink_roots_are_not_deleted(self):
        self.artifacts()
        wrong={**self.device,'api_key':'someone else'}
        with self.assertRaisesRegex(RuntimeError,'ownership'):
            bootstrap.cleanup_job_files(self.root,wrong)
        moved=self.root.with_name('elsewhere')
        self.root.rename(moved)
        self.root.symlink_to(moved,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'ownership'):
            bootstrap.cleanup_job_files(self.root,self.device)
        self.assertTrue((moved/'worker.log').exists())

    def test_job_caches_stay_inside_worker_and_hf_cache_stays_standard(self):
        with patch.dict(os.environ,{'XDG_CACHE_HOME':str(self.home/'xdg'),'HF_HOME':str(self.home/'hf'),'PYTHONPYCACHEPREFIX':'/unrelated/bytecode','UV_CACHE_DIR':'/shared/uv'}):
            env=bootstrap.temporary_environment(self.root,self.device)
        self.assertEqual(env['HF_HUB_CACHE'],str(self.home/'hf/hub'))
        self.assertEqual(env['HF_HOME'],str(self.home/'hf'))
        self.assertEqual(env['UV_LINK_MODE'],'copy')
        self.assertNotIn('PYTHONPYCACHEPREFIX',env)
        for key in ('TMPDIR','UV_CACHE_DIR','PIP_CACHE_DIR','HF_MODULES_CACHE','HF_XET_CACHE','TORCHINDUCTOR_CACHE_DIR','CUDA_CACHE_PATH'):
            self.assertTrue(Path(env[key]).is_relative_to(self.root),key)
        bootstrap.cleanup_job_files(self.root,self.device)
        self.assertTrue(self.python.exists())
        self.assertFalse((self.root/'cache').exists())

    def test_stopped_and_missing_pid_both_clean_artifacts_and_reuse_venv(self):
        self.artifacts()
        (self.root/'worker.pid').write_text('123')
        procs=self.home/'proc'
        procs.mkdir()
        with patch.object(bootstrap,'PROCESSES',procs), patch.object(bootstrap.os,'kill') as kill:
            bootstrap.stop_owned_worker(self.root,self.device,lambda *args,**kwargs:None)
        kill.assert_not_called()
        self.assertTrue(self.python.exists())
        self.assertFalse((self.root/'worker.log').exists())
        bootstrap.stop_owned_worker(self.root,self.device,lambda *args,**kwargs:None)
        with patch.object(bootstrap.subprocess,'run') as install:
            self.assertEqual(bootstrap.prepare_runtime(self.root,lambda *args,**kwargs:None),self.python)
        install.assert_not_called()

    def test_stop_waits_for_verified_worker_before_removing_private_data(self):
        self.artifacts()
        (self.root/'worker.pid').write_text('123')
        (self.root/'worker.json').write_text(json.dumps({'gpu_uuid':'GPU-test'}))
        process=self.home/'proc/123'
        process.mkdir(parents=True)
        (process/'cwd').symlink_to(self.root)
        (process/'environ').write_bytes(b'CUDA_VISIBLE_DEVICES=GPU-test\0COSMOSS_DEVICE_ID='+self.device['id'].encode()+b'\0COSMOSS_WORKER_TOKEN=test-key\0')
        def terminate(pid,sig):
            self.assertEqual((pid,sig),(123,signal.SIGTERM))
            self.assertTrue((self.root/'tmp/private-recording.wav').exists())
            shutil.rmtree(process)
        with patch.object(bootstrap,'PROCESSES',self.home/'proc'),patch.object(bootstrap.os,'kill',side_effect=terminate):
            bootstrap.stop_owned_worker(self.root,self.device,lambda *args,**kwargs:None)
        self.assertFalse((self.root/'tmp').exists())
        self.assertTrue(self.python.exists())

    def test_install_failure_removes_download_artifacts_but_keeps_environment(self):
        def fail(shared,device,emit):
            (self.root/'tmp/private.wav').write_bytes(b'audio')
            raise RuntimeError('installation failed')
        request={'device':self.device,'action':'install'}
        with patch.object(bootstrap.sys,'stdin',io.StringIO(json.dumps(request))),patch.object(bootstrap.sys,'stdout',io.StringIO()),patch.object(bootstrap,'prepare_shared_runtime',side_effect=fail),self.assertRaisesRegex(RuntimeError,'installation failed'):
            bootstrap.main()
        self.assertFalse((self.root/'cache').exists())
        self.assertFalse((self.root/'tmp').exists())
        self.assertTrue(self.python.exists())

    def test_worker_startup_failure_cleans_source_log_pid_and_caches(self):
        import urllib.error
        process=MagicMock(pid=123)
        process.poll.return_value=1
        port=MagicMock()
        port.__enter__.return_value.connect_ex.return_value=1
        request={'device':self.device,'action':'prepare','source':'# generated worker'}
        gpu=b'<nvidia_smi_log><gpu><uuid>GPU-test</uuid><processes/></gpu></nvidia_smi_log>'
        with patch.object(bootstrap.sys,'stdin',io.StringIO(json.dumps(request))),patch.object(bootstrap.sys,'stdout',io.StringIO()), \
             patch.object(bootstrap.urllib.request,'urlopen',side_effect=urllib.error.URLError('not running')), \
             patch.object(bootstrap.socket,'socket',return_value=port),patch.object(bootstrap.subprocess,'check_output',return_value=gpu), \
             patch.object(bootstrap,'prepare_runtime',return_value=self.python), \
             patch.object(bootstrap,'prepare_model',return_value={'model_path':str(self.model.parent),'model_cache':str(self.home/'hf/hub')}), \
             patch.object(bootstrap.subprocess,'Popen',return_value=process),self.assertRaisesRegex(RuntimeError,'Worker exited'):
            bootstrap.main()
        self.assertTrue(self.python.exists())
        self.assertTrue(self.model.exists())
        self.assertFalse(any((self.root/name).exists() for name in bootstrap.JOB_FILES))

    def test_live_worker_files_are_kept_when_signals_do_not_stop_it(self):
        self.artifacts()
        (self.root/'worker.pid').write_text('123')
        process=self.home/'proc/123'
        process.mkdir(parents=True)
        (process/'cwd').symlink_to(self.root)
        (process/'environ').write_bytes(b'CUDA_VISIBLE_DEVICES=GPU-test\0COSMOSS_DEVICE_ID='+self.device['id'].encode()+b'\0COSMOSS_WORKER_TOKEN=test-key\0')
        with patch.object(bootstrap,'PROCESSES',self.home/'proc'),patch.object(bootstrap.os,'kill') as kill,patch.object(bootstrap.time,'sleep'),self.assertRaisesRegex(RuntimeError,'still running'):
            bootstrap.stop_owned_worker(self.root,self.device,lambda *args,**kwargs:None)
        self.assertEqual(kill.call_count,2)
        self.assertTrue((self.root/'tmp/private-recording.wav').exists())


class CleanupStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_failure_is_reported_and_success_keeps_confirmation(self):
        workers=RemoteWorkers()
        device={'id':'a'*32}
        workers.states[device['id']]={'phase':'connecting','action':'stop'}
        with patch.object(workers,'execute',new=AsyncMock(side_effect=RuntimeError('SSH unavailable'))):
            await workers.run(device,'stop')
        self.assertEqual(workers.states[device['id']]['phase'],'error')
        self.assertIn('정리하지 못했습니다',workers.states[device['id']]['error'])
        workers.states[device['id']]={'phase':'connecting','action':'stop'}
        async def success(device,action,on_event):on_event({'phase':'stopped','cleaned':True})
        with patch.object(workers,'execute',new=success):await workers.run(device,'stop')
        self.assertTrue(workers.states[device['id']]['cleaned'])
