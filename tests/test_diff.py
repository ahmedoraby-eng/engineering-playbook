from ai_guard.diff import parse_diff

SAMPLE = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@ def main():
     keep()
-    old()
+    new()
+    newer()
     tail()
diff --git a/docs/old.md b/docs/old.md
deleted file mode 100644
--- a/docs/old.md
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-gone too
"""


def test_added_lines_keep_their_new_file_line_numbers():
    changes = parse_diff(SAMPLE)
    app = changes[0]
    assert app.path == "src/app.py"
    assert app.added == [(11, "    new()"), (12, "    newer()")]
    assert app.removed == 1
    assert app.changed_lines == 3


def test_deleted_files_are_flagged():
    deleted = parse_diff(SAMPLE)[1]
    assert deleted.path == "docs/old.md"
    assert deleted.deleted
    assert deleted.removed == 2


def test_empty_diff_has_no_changes():
    assert parse_diff("") == []
