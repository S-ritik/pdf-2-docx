"""Seed glossary.json from the Orissa Records Manual 19-page edge-case run."""
import glossary as G

PATH = "glossary.json"
g = G.load(PATH)

# (page, token, note) -- confirmed CORRECT as printed; must never be "fixed"
PRESERVE = [
    (47,  "no ensure economy",          "garbled clause, reproduced as printed"),
    (47,  "invaribly-sir or Madam",     "source spelling/hyphenation"),
    (91,  "Archieves",                  "archaic spelling in heading 178; body uses 'Archives'"),
    (91,  "perquisite",                 "printed where 'prerequisite' is meant"),
    (91,  "Sub-ordinate",               "hyphenation as printed"),
    (141, "Gazetters",                  "source spelling (set in italic)"),
    (143, "labelling to books",         "source wording (not 'of books')"),
    (169, "naye paise",                 "currency spelling as printed"),
    (182, "insectides",                 "source spelling"),
    (182, "archaelogical",              "source spelling"),
    (182, "Air Craft",                  "two words as printed"),
    (182, "Non- Gazetted",              "space after hyphen as printed"),
    (240, "expeditions",                "source spelling (cf. 'expedition') in the N.B. note"),
    (221, "APPENDIX – F",          "spaced en-dash on this page; other appendix pages use 'APPENDIX-F'"),
    (298, "Do...",                      "ditto placeholder; keep trailing dots verbatim"),
    (298, "Reg. 2...",                  "partial register no.; trailing dots verbatim"),
    (298, "Reg. 58...",                 "partial register no.; trailing dots verbatim"),
    (298, "Reg. 25...",                 "partial register no.; trailing dots verbatim"),
    (298, "O. F.A. Act",               "spacing as printed"),
    (298, "L. No.4646 (13)-L.R.",       "reference string as printed"),
    (298, "O.T.C. Vol. I. Rule 618",    "reference string as printed"),
    (298, "5827-F",                     "notification no. as printed"),
    (298, "130-A",                      "rule no. as printed"),
]

# (page, wrong, right, note) -- confirmed OCR fixes
CORRECTIONS = [
    (91, "centrally,leaving", "centrally, leaving", "missing space after comma"),
    (91, "transferred  must", "transferred must",   "double space collapsed"),
]

for pg, text, note in PRESERVE:
    G.add_preserve(g, text, note, pg)
for pg, wrong, right, note in CORRECTIONS:
    G.add_correction(g, wrong, right, note, pg)

G.save(g, PATH)
print(f"seeded -> {PATH}: {len(g['preserve'])} preserve, {len(g['corrections'])} corrections")
print("conflicts:", G.conflicts(g) or "none")
