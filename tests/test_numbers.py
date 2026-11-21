# ruff: noqa: PT009, PT027
import unittest

from scripts.numeric import abstract_and_body, compare, consistency, extract


def vals(text, kind=None):
    return [(n["value"], n["kind"]) for n in extract(text) if kind is None or n["kind"] == kind]


class Extract(unittest.TestCase):
    def test_percent_forms(self):
        self.assertEqual(vals("rose 3.40% and 3.40 % and 19.6 percent"), [(3.4, "percent")] * 2 + [(19.6, "percent")])
        self.assertEqual(extract("rose 3.40%")[0]["decimals"], 2)
        self.assertEqual(extract("a 30% reduction in risk")[0]["unit"], "reduction in")

    def test_scale_suffixes_and_negatives(self):
        v = [n["value"] for n in extract("1,234 items, 1.2k users, 2 million rows, change of -3.5 points")]
        self.assertEqual(v, [1234, 1200, 2000000, -3.5])

    def test_multiplier(self):
        self.assertEqual(vals("a ×3 speedup and 3x faster", "multiplier"), [(3.0, "multiplier")] * 2)

    def test_ci_ranges(self):
        for text in ("OR 1.07 (95% CI 0.50--2.26)", "OR 1.07 (95% CI 0.50–2.26)"):
            n = extract(text)
            self.assertEqual([(x["value"], x["kind"]) for x in n], [(1.07, "ratio"), (0.5, "ci_low"), (2.26, "ci_high")])

    def test_skips_years_sections_citations(self):
        text = "As shown in Section 3.2 and Table 4 [12], Smith et al. (2019) found 42 patients in March 2020 [3, 5]."
        self.assertEqual([n["value"] for n in extract(text)], [42])
        self.assertEqual([n["value"] for n in extract("1. Introduction\n2.1 Data\nWe use GPT-4 with 7 runs.")], [7])


class Compare(unittest.TestCase):
    def test_exact_and_rounded(self):
        r = compare("Accuracy was 3.4%.", "Accuracy was 3.40% overall.")
        self.assertEqual(r["numbers"][0]["status"], "exact")
        r = compare("About 19.6% responded.", "Response rate: 19.64% of invitees")
        self.assertEqual(r["numbers"][0]["status"], "rounded")

    def test_derived_percentage_from_counts(self):
        r = compare("Failure rate was 3.40%.", "Of 500 runs, 17 failed.")
        n = r["numbers"][0]
        self.assertEqual(n["status"], "derived")
        self.assertIn("17/500 = 3.40%", n["derived"])
        self.assertEqual(r["summary"]["derived"][0]["value"], "3.40%")

    def test_percent_change_and_pp_difference(self):
        r = compare("Error fell by 20.0% relative.", "Error dropped from 50.0 to 40.0 on the test set.")
        self.assertIn("(40-50)/50", r["numbers"][0]["derived"][0])
        r = compare("an improvement of 4.5 percentage points", "Accuracy rose from 80.5% to 85.0%.")
        self.assertIn("percentage points", r["numbers"][0]["derived"][0])

    def test_mismatch_candidate(self):
        r = compare("a 30% reduction in mortality", "We observed a 13% reduction in mortality.")
        n = r["numbers"][0]
        self.assertEqual(n["status"], "mismatch")
        self.assertEqual(n["mismatch_candidates"][0]["text"], "13%")
        self.assertEqual(r["summary"]["mismatch"][0]["source_values"], ["13%"])

    def test_absent(self):
        r = compare("Latency was 87 ms.", "The weather was mild.")
        self.assertEqual(r["numbers"][0]["status"], "absent")
        self.assertEqual(r["summary"]["absent"], ["87"])

    def test_large_source_is_capped(self):
        src = " ".join(f"{i}.{i % 10}1 widgets" for i in range(3000))
        r = compare("Result was 7.77 widgets and 12%", src)
        self.assertEqual(len(r["numbers"]), 2)


class Consistency(unittest.TestCase):
    def test_abstract_number_missing(self):
        out = consistency({"abstract": "Our method improves F1 by 12.5%.", "body": "F1 improved by 8.1% on average."})
        self.assertEqual([f["kind"] for f in out], ["abstract_number_missing"])
        self.assertEqual(out[0]["value"], "12.5%")
        self.assertEqual(out[0]["where"], "abstract")

    def test_inconsistent_restatement(self):
        out = consistency(
            {
                "abstract": "The model reduces training time by 40% on large datasets.",
                "body": "The model reduces training time by 25% on large datasets.",
            }
        )
        self.assertEqual([f["kind"] for f in out], ["inconsistent_number"])
        self.assertEqual(out[0]["other"]["value"], "25%")

    def test_no_false_alarm(self):
        out = consistency(
            {
                "abstract": "We reach 91.2% accuracy on 1,200 images, about 3x faster, in 2021.",
                "conclusion": "Accuracy of 91% holds across 3 runs.",
                "body": "Accuracy is 91.24% on 1200 images. Speedup is 3.0x. We used 5 seeds.",
            }
        )
        self.assertEqual(out, [])


class Sections(unittest.TestCase):
    def test_latex(self):
        tex = (
            "\\documentclass{article}\\begin{document}\\begin{abstract}We win 5.5\\%.\\end{abstract}\n"
            "\\section{Intro}Body text 7.\n\\section{Conclusions}We conclude 5.5\\%.\n"
            "\\bibliography{x}\\end{document}"
        )
        r = abstract_and_body(tex, latex=True)
        self.assertEqual(r["abstract"], "We win 5.5\\%.")
        self.assertIn("Body text", r["body"])
        self.assertNotIn("conclude", r["body"])
        self.assertEqual(r["conclusion"], "We conclude 5.5\\%.")

    def test_markdown_and_missing(self):
        md = "# Title\n\n## Abstract\n\nWe win 5%.\n\n## 1 Introduction\n\nText.\n\n## Conclusion\n\nDone 5%.\n\n## References\n\n[1] x"
        r = abstract_and_body(md, latex=False)
        self.assertEqual(r["abstract"], "We win 5%.")
        self.assertEqual(r["conclusion"], "Done 5%.")
        self.assertNotIn("[1] x", r["body"])
        self.assertEqual(abstract_and_body("Just text 5%.", latex=False)["abstract"], "")


if __name__ == "__main__":
    unittest.main()
