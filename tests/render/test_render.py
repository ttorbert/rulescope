"""Tests for workflow generation (rulescope/render.py) and the bulk row-position lint check."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rulescope import render  # noqa: E402
from rulescope.core import WORKFLOW_FILES, WORKFLOW_TEMPLATE  # noqa: E402
from rulescope.lint import bulk_row_problems, keep_columns  # noqa: E402
from rulescope.resources import render_workflow  # noqa: E402


class TemplateTests(unittest.TestCase):
    def r(self, text, scope="global"):
        return render.render_template(text, scope)

    def test_if_else(self):
        t = "a\n#% if global\ng\n#% else\ns\n#% end\nz\n"
        self.assertEqual(self.r(t, "global"), "a\ng\nz\n")
        self.assertEqual(self.r(t, "space"), "a\ns\nz\n")

    def test_indent_applies_in_its_scope_only_and_skips_blank_lines(self):
        t = "#% indent global 4\n- x\n\n  y\n#% end\n"
        self.assertEqual(self.r(t, "global"), "    - x\n\n      y\n")
        self.assertEqual(self.r(t, "space"), "- x\n\n  y\n")

    def test_nested_indent_and_if(self):
        t = "#% indent space 2\n#% if space\nk\n#% end\n#% end\n"
        self.assertEqual(self.r(t, "space"), "  k\n")
        self.assertEqual(self.r(t, "global"), "")

    def test_variables(self):
        t = "#% var who global=foreach.item.id space=consts.space_id\nid: {{ [[who]] }}\n"
        self.assertEqual(self.r(t, "global"), "id: {{ foreach.item.id }}\n")
        self.assertEqual(self.r(t, "space"), "id: {{ consts.space_id }}\n")

    def test_liquid_and_notes_pass_through(self):
        t = "#% # a note\n{% if x %}{{ row[0] | json }}{% endif %}\n"
        self.assertEqual(self.r(t), "{% if x %}{{ row[0] | json }}{% endif %}\n")

    def test_errors(self):
        for t in ("#% bogus\n", "#% if global\nx\n", "#% else\n", "#% end\n", "[[nope]]\n",
                  "#% var v global=x\n", "#% if both\n#% end\n", "#% indent global x\n#% end\n", "#%\n"):
            with self.subTest(t=t), self.assertRaises(render.TemplateError):
                self.r(t)

    def test_unknown_scope(self):
        with self.assertRaises(render.TemplateError):
            self.r("x\n", "cluster")


class LagWorkflowTests(unittest.TestCase):
    def test_template_reproduces_checked_in_files_exactly(self):
        for mode, path in WORKFLOW_FILES.items():
            with self.subTest(mode=mode):
                got = render.render_file(WORKFLOW_TEMPLATE, render.MODE_SCOPES[mode])
                self.assertEqual(got, (ROOT / path).read_text())
        self.assertEqual(render.stale_workflows(), [])

    def test_installer_renders_from_template(self):
        _, text = render_workflow("per-space", "soc", shared_stats=True)
        self.assertIn("space_id: soc", text)
        self.assertNotIn("#%", text)
        self.assertNotIn("[[", text)

    def test_every_bulk_body_is_checked(self):
        for text in render.generated_workflows().values():
            self.assertEqual(text.count(".output.values %}"), 3)
            self.assertEqual(bulk_row_problems(text), [])


class RowPositionTests(unittest.TestCase):
    WF = """\
steps:
  - name: q
    with:
      body:
        query: |
          FROM x
          | KEEP window_start, lagprof.rule.id,
                 events
          | LIMIT 10
        params:
          - a: b
  - name: save
    with:
      body: |
        {% for row in steps.q.output.values %}{"create":{"_id":"{{ row[0] }}"}}
        {"@timestamp":{{ row[0] | json }},"window":{"start":{{ row[0] | json }}},"id":{{ row[1] | json }},"events":{{ row[2] | json }}}
        {% endfor %}
"""

    def test_keep_columns(self):
        q = "FROM x\n| KEEP a, b.c,\n       d\n| SORT a\n"
        self.assertEqual(keep_columns(q), ["a", "b.c", "d"])
        self.assertEqual(keep_columns("FROM x | LIMIT 1"), None)
        self.assertEqual(keep_columns("| KEEP a,\n  b\nparams:\n  - x: y\n"), ["a", "b"])

    def test_matching_body_passes(self):
        self.assertEqual(bulk_row_problems(self.WF), [])

    def test_swapped_columns(self):
        bad = self.WF.replace('"id":{{ row[1]', '"id":{{ row[2]').replace('"events":{{ row[2]', '"events":{{ row[1]')
        msgs = bulk_row_problems(bad)
        self.assertEqual(len(msgs), 2, msgs)
        self.assertIn('"events" is written from row[1]', msgs[1])

    def test_index_past_keep(self):
        msgs = bulk_row_problems(self.WF.replace('"events":{{ row[2]', '"events":{{ row[3]'))
        self.assertTrue(any("keeps only 3 columns" in m for m in msgs), msgs)
        self.assertTrue(any("events (row[2]) is never written" in m for m in msgs), msgs)

    def test_query_without_keep(self):
        msgs = bulk_row_problems(self.WF.replace("| KEEP", "| EVAL"))
        self.assertEqual(msgs, ["rows of q are written, but q has no query ending in KEEP"])


if __name__ == "__main__":
    unittest.main()
