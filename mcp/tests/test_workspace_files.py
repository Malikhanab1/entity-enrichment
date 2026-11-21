import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrichment_mcp.tools import workspace_files


class WorkspaceFileToolsTests(unittest.TestCase):
    def test_text_create_search_replace_and_traversal_block(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(workspace_files, "WORKSPACE", Path(directory)):
            created = workspace_files.write_text_file("notes/brief.md", "# Acme\nOwner: Sam\n")
            found = workspace_files.search_files("owner", pattern="*.md")
            found_at_root = workspace_files.search_files("owner", path="", pattern="*.md")
            changed = workspace_files.replace_in_text_file("notes/brief.md", "Sam", "Ava", expected_replacements=1)
            read = workspace_files.read_text_file("notes/brief.md")

            self.assertTrue(created["created"])
            self.assertEqual(found["matches"][0]["line"], 2)
            self.assertEqual(found_at_root["matches"][0]["line"], 2)
            self.assertEqual(changed["replacements"], 1)
            self.assertIn("Ava", read["content"])
            with self.assertRaises(ValueError):
                workspace_files.read_text_file("../../etc/passwd")

    def test_csv_and_xlsx_round_trip(self):
        rows = [["Name", "Score"], ["Acme", 80]]
        with tempfile.TemporaryDirectory() as directory, patch.object(workspace_files, "WORKSPACE", Path(directory)):
            workspace_files.write_spreadsheet("leads.csv", rows)
            workspace_files.write_spreadsheet("leads.xlsx", rows)
            csv_data = workspace_files.read_spreadsheet("leads.csv")
            xlsx_data = workspace_files.read_spreadsheet("leads.xlsx")

        self.assertEqual(csv_data["rows"], [["Name", "Score"], ["Acme", "80"]])
        self.assertEqual(xlsx_data["rows"], rows)

    def test_csv_spreadsheet_patches_add_update_and_append_rows(self):
        rows = [["Name", "Score"], ["Acme", "80"], ["Beta", "60"]]
        with tempfile.TemporaryDirectory() as directory, patch.object(workspace_files, "WORKSPACE", Path(directory)):
            workspace_files.write_spreadsheet("leads.csv", rows)
            added = workspace_files.add_spreadsheet_column("leads.csv", "Status", "new")
            updated = workspace_files.update_spreadsheet_rows(
                "leads.csv", "Name", "Acme", {"Score": 95, "Status": "qualified"}, expected_matches=1
            )
            appended = workspace_files.append_spreadsheet_rows(
                "leads.csv", [{"Name": "Gamma", "Score": 75, "Status": "new"}]
            )
            data = workspace_files.read_spreadsheet("leads.csv")

        self.assertEqual(added["column_added"], "Status")
        self.assertEqual(updated["rows_updated"], 1)
        self.assertEqual(appended["rows_appended"], 1)
        self.assertEqual(
            data["rows"],
            [["Name", "Score", "Status"], ["Acme", "95", "qualified"], ["Beta", "60", "new"], ["Gamma", "75", "new"]],
        )

    def test_xlsx_patch_preserves_other_sheets_and_formulas(self):
        from openpyxl import Workbook, load_workbook

        with tempfile.TemporaryDirectory() as directory, patch.object(workspace_files, "WORKSPACE", Path(directory)):
            path = Path(directory) / "leads.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "Leads"
            sheet.append(["Name", "Score", "Total"])
            sheet.append(["Acme", 80])
            sheet["C2"] = "=B2+20"
            notes = book.create_sheet("Notes")
            notes["A1"] = "Keep me"
            book.save(path)

            workspace_files.add_spreadsheet_column("leads.xlsx", "Status", "new", sheet="Leads")
            workspace_files.update_spreadsheet_rows("leads.xlsx", "Name", "Acme", {"Status": "qualified"}, sheet="Leads")
            workspace_files.append_spreadsheet_rows(
                "leads.xlsx", [{"Name": "Beta", "Score": 70, "Status": "new"}], sheet="Leads"
            )
            patched = load_workbook(path, data_only=False)

        self.assertEqual(patched["Leads"]["C2"].value, "=B2+20")
        self.assertEqual(patched["Leads"]["D2"].value, "qualified")
        self.assertEqual(patched["Leads"]["A3"].value, "Beta")
        self.assertEqual(patched["Notes"]["A1"].value, "Keep me")
