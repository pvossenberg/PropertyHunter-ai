import ast
import unittest
from pathlib import Path


class DealFinderRenderingSafetyTests(unittest.TestCase):
    def test_deal_finder_rendering_has_no_dataframe_calls(self):
        app_path = Path("/workspaces/PropertyHunter-ai/app.py")
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        function_names = {"_render_deal_finder_page", "_render_analysis_result", "_render_rows_with_columns", "_render_deal_candidate_cards"}
        function_sources = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in function_names:
                snippet = ast.get_source_segment(source, node) or ""
                function_sources.append(snippet)

        self.assertEqual(len(function_sources), len(function_names))
        combined = "\n".join(function_sources)
        self.assertNotIn("st.dataframe", combined)
        self.assertNotIn("DataFrame", combined)
        self.assertNotIn("Styler", combined)
        self.assertNotIn("numpy", combined)
        self.assertNotIn("pyarrow", combined)
        self.assertNotIn("pandas", combined)

    def test_app_module_has_no_pandas_or_pyarrow_imports(self):
        app_path = Path("/workspaces/PropertyHunter-ai/app.py")
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        joined = "\n".join(imported).lower()
        self.assertNotIn("pandas", joined)
        self.assertNotIn("pyarrow", joined)


if __name__ == "__main__":
    unittest.main()
