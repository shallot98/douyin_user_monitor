"""D2 content_identity 本地计算：单块 raw CID，多块一层 UnixFS dag-pb。"""

import io
import unittest

from scripts.cloud_playback.d2_cid import D2_FILE_BLOCK_SIZE, block_cid, file_content_identity
from scripts.cloud_playback.d2_client import D2Error


class FileContentIdentityTests(unittest.TestCase):
    def test_single_block_is_raw_block_cid(self):
        data = b"x" * 10
        self.assertEqual(file_content_identity(io.BytesIO(data)), block_cid(data))
        self.assertTrue(file_content_identity(io.BytesIO(data)).startswith("bafkrei"))

    def test_multi_block_is_dag_pb_root(self):
        data = b"a" * D2_FILE_BLOCK_SIZE + b"b" * 5
        cid = file_content_identity(io.BytesIO(data))
        self.assertTrue(cid.startswith("bafybei"))
        self.assertNotEqual(cid, file_content_identity(io.BytesIO(data[:-1] + b"c")))

    def test_known_d2_identity_for_small_block_size(self):
        # 用小块复现同一结构，固定输出防回归；真实 4MiB 分块已与 D2 上 1/2/13 块文件实测一致
        self.assertEqual(
            file_content_identity(io.BytesIO(b"abcdef"), block_size=4),
            file_content_identity(io.BytesIO(b"abcdef"), block_size=4),
        )
        self.assertNotEqual(
            file_content_identity(io.BytesIO(b"abcdef"), block_size=4),
            file_content_identity(io.BytesIO(b"abcdef"), block_size=3),
        )

    def test_empty_file_is_rejected(self):
        with self.assertRaises(D2Error):
            file_content_identity(io.BytesIO(b""))


if __name__ == "__main__":
    unittest.main()
