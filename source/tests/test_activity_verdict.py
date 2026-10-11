"""The status verdict must not mistake a running effect for residue."""
import unittest
import friendswine as fw


class ActivityVerdictTests(unittest.TestCase):
    def verdict(self, **fields):
        return fw.activity_verdict('status', fields) or ''

    def test_running_with_modified_models_is_normal(self):
        text = self.verdict(phase='RUNNING', modified=5, unresolved=0, pending_restores=0)
        self.assertIn('诊断结果正常', text)
        self.assertIn('5 个模型', text)
        self.assertNotIn('仍有未恢复', text)

    def test_running_pending_restore_is_not_residue(self):
        text = self.verdict(phase='RUNNING', modified=3, unresolved=0, pending_restores=1)
        self.assertIn('诊断结果正常', text)
        self.assertNotIn('仍有未恢复', text)

    def test_unresolved_while_running_is_reported(self):
        text = self.verdict(phase='RUNNING', modified=1, unresolved=1, pending_restores=0)
        self.assertIn('无法确认恢复', text)

    def test_stopped_residue_points_at_cleanup(self):
        text = self.verdict(phase='RESTORE_FAULT', modified=1, unresolved=1, pending_restores=0)
        self.assertIn('仍有未恢复', text)
        self.assertIn('错误修复', text)

    def test_waiting_for_scene_mentions_match_only(self):
        text = self.verdict(phase='WAITING_FOR_SCENE')
        self.assertIn('匹配对局', text)
        self.assertNotIn('单人训练', text)

    def test_clean_stop_reads_as_normal(self):
        self.assertIn('没有运行中的模组', self.verdict(phase='STOPPED', modified=0))


if __name__ == '__main__':
    unittest.main()
