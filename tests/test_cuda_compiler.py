"""Catch mixed CUDA components before any model/GPU work starts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.speech_engine_bootstrap import check_compiler, compiler_ready, CUDA_COMPILER_PACKAGES, CUDA_COMPILER_PROBE, CUDA_COMPILER_VERSION
from src.speech_engines import REQUIREMENTS


class CompilerTests(unittest.TestCase):
    def test_mixed_nvvm_is_rejected_before_compilation(self):
        for nvvm in (CUDA_COMPILER_VERSION, '13.5.37'):
            with self.subTest(nvvm=nvvm), tempfile.TemporaryDirectory() as folder:
                calls=[]
                def run(args, **kwargs):
                    calls.append((args,kwargs))
                    if len(calls)==1:
                        with patch('importlib.metadata.version', side_effect=lambda p: nvvm if p=='nvidia-nvvm' else CUDA_COMPILER_VERSION):
                            exec(args[-1], {})
                if nvvm=='13.5.37':
                    with self.assertRaises(AssertionError):
                        check_compiler(Path('/python'),Path('/private/cuda'),Path(folder),{'PATH':'/usr/bin'},run)
                    self.assertEqual(len(calls),1)
                else:
                    check_compiler(Path('/python'),Path('/private/cuda'),Path(folder),{'PATH':'/usr/bin'},run)
                    self.assertEqual(calls[1][0][0],'/private/cuda/bin/nvcc')
                    self.assertEqual(calls[1][1]['env']['CUDA_HOME'],'/private/cuda')
                    self.assertNotIn('stage',calls[1][1])

    def test_assembler_failure_has_repair_message(self):
        with tempfile.TemporaryDirectory() as folder:
            def run(args,**kwargs):
                if args[0].endswith('nvcc'): raise RuntimeError('Unsupported .version 9.5')
            with self.assertRaisesRegex(RuntimeError,'컴파일 도구 호환성'):
                check_compiler(Path('/python'),Path('/cuda'),Path(folder),{'PATH':'/usr/bin'},run)

    def test_cuda_runtime_version_does_not_block_matching_compiler(self):
        with tempfile.TemporaryDirectory() as folder:
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args[0]=='/python':
                    with patch('importlib.metadata.version', side_effect=lambda p: '13.0.96' if p=='nvidia-cuda-runtime' else CUDA_COMPILER_VERSION):
                        exec(args[-1], {})
            check_compiler(Path('/python'),Path('/cuda'),Path(folder),{'PATH':'/usr/bin'},run)
            self.assertEqual(len(calls),2)
            self.assertEqual(CUDA_COMPILER_PACKAGES,('nvidia-cuda-nvcc','nvidia-nvvm','nvidia-cuda-crt'))

    def test_legacy_marker_never_means_compiler_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'ready').write_text('old engine')
            self.assertFalse(compiler_ready(root))
            (root/'cuda-toolkit/bin').mkdir(parents=True);(root/'cuda-toolkit/bin/nvcc').touch()
            (root/'cuda-toolchain-ready').write_text(CUDA_COMPILER_PROBE)
            with patch('src.speech_engine_bootstrap.shutil.which', return_value=None):
                self.assertFalse(compiler_ready(root))
            (root/'bin').mkdir()
            ninja = root/'bin/ninja'
            ninja.touch(mode=0o700)
            self.assertTrue(compiler_ready(root))
        for package in ('nvidia-nvvm','nvidia-cuda-nvcc','nvidia-cuda-crt'):
            self.assertIn(package+'==13.0.88',REQUIREMENTS['sglang'])
        self.assertIn('nvidia-cuda-runtime==13.0.96',REQUIREMENTS['sglang'])
        self.assertIn('ninja==1.13.0',REQUIREMENTS['sglang'])
