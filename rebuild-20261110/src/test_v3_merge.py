import unittest
from v3_merge import canonical,merge

class MergeTests(unittest.TestCase):
 def test_disjoint_edits_merge(self):
  result=merge(b'a\nb\nc\n',b'A\nb\nc\n',b'a\nb\nC\n')
  self.assertFalse(result.conflict);self.assertEqual(result.content,b'A\nb\nC\n')
 def test_same_paragraph_edits_conflict(self):
  result=merge(b'a\nb\n',b'A\nb\n',b'other\nb\n')
  self.assertTrue(result.conflict);self.assertIsNone(result.content)
 def test_same_position_insertions_do_not_hang_or_drop_content(self):
  result=merge(b'a\n',b'L\na\n',b'R\na\n')
  self.assertTrue(result.conflict);self.assertIsNone(result.content)
 def test_identical_insertions_preserved(self):
  result=merge(b'a\nb\n',b'new\na\nb\n',b'new\na\nB\n')
  self.assertFalse(result.conflict);self.assertEqual(result.content,b'new\na\nB\n')
 def test_disjoint_insertions(self):
  result=merge(b'a\nb\nc\n',b'local\na\nb\nc\n',b'a\nb\nc\nremote\n')
  self.assertFalse(result.conflict);self.assertEqual(result.content,b'local\na\nb\nc\nremote\n')
 def test_non_destructive_comparison(self):
  self.assertEqual(canonical('e\u0301\r\n'),'é\n')
  self.assertNotEqual(canonical('a  \n'),canonical('a\n'))
  result=merge(b'a\nb\n',b'a\r\nb\r\n',b'a\nB\n')
  self.assertTrue(result.conflict)
 def test_canonical_equal_does_not_rewrite_local_bytes(self):
  result=merge(b'old', 'é\r\n'.encode(), 'e\u0301\n'.encode())
  self.assertFalse(result.conflict);self.assertEqual(result.content,'é\r\n'.encode())
 def test_unsupported_bytes_conflict(self):
  self.assertTrue(merge(b'a',b'\xff',b'b').conflict)

if __name__=='__main__':unittest.main()
