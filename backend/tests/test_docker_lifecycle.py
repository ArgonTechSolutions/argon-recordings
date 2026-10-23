"""No daemon needed: temporary Docker clients must clean up their own container."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import recordings


class DockerLifecycleTests(unittest.TestCase):
    def test_utility_has_project_function_name_labels_and_cleanup(self):
        with patch.object(recordings.subprocess, 'run') as run:
            recordings.docker({'sudo': False}, 'run', '--rm', 'test-image', 'scripts/prepare_turbo.py', '/model')
        command = run.call_args_list[0].args[0]
        name = command[command.index('--name') + 1]
        self.assertRegex(name, r'^argon-recordings-prepare-turbo-[0-9a-f]{12}$')
        self.assertIn('com.argon.recordings.project=argon-recordings', command)
        self.assertFalse(any('com.argon.recordings.installation=' in value for value in command))
        self.assertEqual(run.call_args_list[1].args[0], ['docker', 'rm', '-f', name])

    def test_failure_or_cancellation_removes_only_the_generated_container(self):
        for error in [subprocess.CalledProcessError(1, ['docker']), KeyboardInterrupt()]:
            with self.subTest(error=type(error).__name__), patch.object(recordings.subprocess, 'run', side_effect=[error, None]) as run:
                with self.assertRaises(type(error)):
                    recordings.docker({'sudo': True}, 'run', '--rm', 'test-image')
                command = run.call_args_list[0].args[0]
                name = command[command.index('--name') + 1]
                self.assertEqual(run.call_args_list[1].args[0], ['sudo', '-n', 'docker', 'rm', '-f', name])

    def test_named_or_persistent_containers_keep_the_callers_lifecycle(self):
        for arguments in [('run', '-d', '--name', 'argon-recordings', 'test-image'),
                          ('run', '--rm', '--name=argon-recordings-custom', 'test-image'),
                          ('inspect', 'argon-recordings')]:
            with self.subTest(arguments=arguments), patch.object(recordings.subprocess, 'run') as run:
                recordings.docker({'sudo': False}, *arguments)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ['docker', *arguments])

    def test_concurrent_utility_calls_get_distinct_names(self):
        with patch.object(recordings.subprocess, 'run') as run:
            for _ in range(2):
                recordings.docker({'sudo': False}, 'run', '--rm', 'test-image', 'scripts/evaluate_embeddings.py')
        names = [call.args[0][call.args[0].index('--name') + 1] for call in run.call_args_list[::2]]
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(name.startswith('argon-recordings-evaluate-search-') for name in names))


if __name__ == '__main__':
    unittest.main()
