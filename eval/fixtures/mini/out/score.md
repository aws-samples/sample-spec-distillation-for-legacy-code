# Score: `fixtures/mini/spec.md`

- Key: `fixtures/mini/key.yaml` (4 items, 2 core)
- Matcher: **llm**
- Findings parsed: 6, citations: 6

| Metric | Value |
|---|---|
| Recall (full matches) | 75.0% |
| Recall (partial = 0.5) | 75.0% |
| Recall (any match) | 75.0% |
| Core recall (full) | 100.0% |
| Core recall (any) | 100.0% |
| Precision (matched to key) | 50.0% |
| Precision (correct incl. true extras) | 66.7% |
| Incorrect findings | 2 |
| Unsupported findings | 0 |
| Citation validity | 83.3% |
| Citation accuracy (overlaps key lines) | 100.0% |
| Findings without citation | 0 |
| Judge tokens in/out (cost) | 835/235 ($0.0060) |

## Key coverage

| Key item | Type | Core | Coverage |
|---|---|---|---|
| R1 | rule | yes | full |
| R2 | rule | yes | full |
| R3 | rule |  | full |
| D1 | data |  | missed |

## Missed key items

- **D1**: Quantity is an unsigned 3-digit number PIC 9(3).

## Possible inventions / incorrect findings

- **F3** (incorrect) No discount logic exists in the source; ELSE branch simply computes total. 
  > - A 10% discount is applied to orders above 50 units (calc.cob:L12-L13).
- **F4** (incorrect) No ORDERS file or audit write exists; source only displays total to screen. invalid cits: calc.cob:L40-L45
  > - The order total is written to the ORDERS file for audit (calc.cob:L40-L45).

## Invalid citations

- F4: `calc.cob:L40-L45` -> out of range (file has 16 lines)
