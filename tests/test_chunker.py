import unittest

from agentforge.chunker import chunk_text, chunk_signature, split_source_lines


class ChunkerTests(unittest.TestCase):
    def test_original_lines_overlap_and_repeatability(self):
        text = "one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n"
        chunks = chunk_text("src/example.py", text, 3, 1)
        self.assertEqual([(c.start_line, c.end_line, c.text) for c in chunks],
                         [(1, 3, "one\ntwo\nthree"), (3, 5, "three\nfour\nfive")])
        self.assertEqual(chunks, chunk_text("src/example.py", text, 3, 1))
        self.assertEqual(len(chunks[0].content_hash), 64)

    def test_empty_and_single_line(self):
        self.assertEqual(chunk_text("empty.txt", ""), [])
        self.assertEqual(chunk_text("one.txt", "one")[0].end_line, 1)

    def test_only_crlf_lf_and_cr_define_source_lines(self):
        cases = (
            ("a\nb\nc", ["a", "b", "c"]),
            ("a\r\nb\r\nc", ["a", "b", "c"]),
            ("a\rb\rc", ["a", "b", "c"]),
            ("a\fb\nc", ["a\fb", "c"]),
            ("a\vb\u2028c\u2029d\u0085e\nf", ["a\vb\u2028c\u2029d\u0085e", "f"]),
            ("", []),
            ("a", ["a"]),
            ("a\n", ["a"]),
            ("a\n\n", ["a", ""]),
            ("a\n\nb", ["a", "", "b"]),
            ("\n", [""]),
            ("a\r\nb\rc\n", ["a", "b", "c"]),
        )
        for text, expected in cases:
            with self.subTest(text=repr(text)):
                self.assertEqual(split_source_lines(text), expected)

    def test_chunk_ranges_match_independent_source_rows(self):
        rows = ["a\fb", "c", "", "d"]
        text = "a\fb\nc\r\n\r\nd"
        chunks = chunk_text("src/lines.py", text, window_lines=2, overlap_lines=1)
        self.assertEqual(
            [(chunk.start_line, chunk.end_line, chunk.text) for chunk in chunks],
            [(1, 2, "a\fb\nc"), (2, 3, "c\n"), (3, 4, "\nd")],
        )
        for chunk in chunks:
            self.assertEqual(
                chunk.text,
                "\n".join(rows[chunk.start_line - 1:chunk.end_line]),
            )

    def test_chunk_signature_changes_for_line_semantics(self):
        self.assertEqual(chunk_signature(80, 10), "line-breaks-v2:80:10")

    def test_invalid_configuration_and_traversal(self):
        for window, overlap in [(0, 0), (2, 2), (2, -1)]:
            with self.subTest(window=window, overlap=overlap), self.assertRaises(ValueError):
                chunk_text("file.py", "text", window, overlap)
        for path in ("../escape.py", "/absolute.py", "D:/drive.py", "\\rooted.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                chunk_text(path, "text")

    def test_chunk_resource_cap(self):
        with self.assertRaisesRegex(ValueError, "Chunk limit"):
            chunk_text("many.txt", "x\n" * 100001, 1, 0)

