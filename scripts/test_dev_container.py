"""Lifecycle regression checks without starting a service or touching Docker data."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import dev_container as dev


class LauncherTests(unittest.TestCase):
    def test_success_is_named_labelled_and_cleaned(self):
        with patch.object(dev.subprocess, 'Popen') as spawn, patch.object(dev, 'cleanup') as clean, contextlib.redirect_stderr(io.StringIO()):
            spawn.return_value.wait.return_value = 0
            spawn.return_value.poll.return_value = 0
            self.assertEqual(dev.run('backend-tests', ['image', 'true'], 30, ['docker']), 0)
            args = spawn.call_args.args[0]
            self.assertIn('--rm', args)
            self.assertTrue(args[args.index('--name')+1].startswith('argon-recordings-backend-tests-'))
            self.assertIn(dev.PROJECT_LABEL+'=argon-recordings', args)
            clean.assert_called_once()

    def test_timeout_and_interruption_cleanup_only_our_session(self):
        for error, code in [(subprocess.TimeoutExpired('docker', 1), 124), (KeyboardInterrupt(), 130)]:
            with self.subTest(code=code), patch.object(dev.subprocess, 'Popen') as spawn, patch.object(dev, 'cleanup') as clean, contextlib.redirect_stderr(io.StringIO()):
                spawn.return_value.wait.side_effect = error
                spawn.return_value.poll.return_value = 0
                self.assertEqual(dev.run('visual-eval', ['image'], 1, ['docker']), code)
                clean.assert_called_once()

    def test_foreign_container_is_never_stopped(self):
        item = {'Config': {'Labels': {dev.PROJECT_LABEL:'other'}}, 'Id':'foreign', 'State':{'Running':True}}
        with patch.object(dev.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps([item]))) as command:
            with self.assertRaisesRegex(ValueError, 'ownership'):
                dev.cleanup(['docker'],'argon-recordings-check','session')
            self.assertEqual(command.call_count,1)

    def test_reject_lifecycle_overrides_and_other_daemons(self):
        for flag in ['-d','--detach=true','--name=anonymous','--restart=always',dev.PROJECT_LABEL+'=other']:
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                dev.run('check',[flag,'image'],30,['docker'])
        with patch.dict(dev.os.environ, {'DOCKER_HOST':'unix:///var/run/docker.sock'}):
            with self.assertRaises(ValueError): dev.docker_command()
        with patch.dict(dev.os.environ, {}, clear=True), patch.object(dev.subprocess,'check_output',side_effect=['docker-shared\n','unix:///var/run/docker.sock\n']):
            with self.assertRaises(ValueError): dev.docker_command()


if __name__ == '__main__':
    unittest.main()
