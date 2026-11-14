"""Cobbee shut down: new instances never get it, old volumes lose it on boot.

Run:
    python -m unittest tests.test_writer_cobbee
"""

from __future__ import annotations

import os
import tempfile
import unittest

from booa import fetcher, writer

LINE = "- /cobbee — creator platform, x402 payments, USDC earnings\n"
BOOA = {"name": "Test BOOA", "token_id": 1496, "creature": "c", "vibe": "v"}


class CobbeeRemovalTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        writer.ensure_dirs(self.home)
        self.memory = os.path.join(self.home, "memories", "MEMORY.md")

    def test_new_instances_do_not_get_cobbee(self):
        self.assertNotIn("cobbee", fetcher.SKILL_URLS)
        writer.write_seed_memory(self.home, BOOA)
        with open(self.memory) as f:
            self.assertNotIn("cobbee", f.read())

    def test_old_volume_loses_skill_and_seed_line(self):
        skill = os.path.join(self.home, "skills", "cobbee", "references")
        os.makedirs(skill)
        open(os.path.join(skill, "support.md"), "w").close()
        with open(self.memory, "w") as f:
            f.write("# MEMORY\n- /ows — wallet\n" + LINE + "- learned: likes tea\n")

        writer.remove_cobbee(self.home)

        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "cobbee")))
        with open(self.memory) as f:
            self.assertEqual(f.read(), "# MEMORY\n- /ows — wallet\n- learned: likes tea\n")

    def test_does_not_follow_a_symlinked_memory_file(self):
        outside = os.path.join(tempfile.mkdtemp(), "target.md")
        with open(outside, "w") as f:
            f.write("secret\n" + LINE)
        os.symlink(outside, self.memory)

        writer.remove_cobbee(self.home)

        self.assertTrue(os.path.islink(self.memory))
        with open(outside) as f:
            self.assertEqual(f.read(), "secret\n" + LINE)

    def test_nothing_to_remove(self):
        writer.remove_cobbee(self.home)
        self.assertFalse(os.path.exists(self.memory))


if __name__ == "__main__":
    unittest.main()
