# Score: `fixtures/mini/spec.md`

- Key: `fixtures/mini/key.yaml` (4 items, 2 core)
- Matcher: **deterministic**
- Findings parsed: 6, citations: 6

| Metric | Value |
|---|---|
| Recall (full matches) | 75.0% |
| Recall (partial = 0.5) | 75.0% |
| Recall (any match) | 75.0% |
| Core recall (full) | 100.0% |
| Core recall (any) | 100.0% |
| Precision (matched to key) | 50.0% |
| Precision (correct incl. true extras) | n/a |
| Incorrect findings | 0 |
| Unsupported findings | 0 |
| Citation validity | 83.3% |
| Citation accuracy (overlaps key lines) | 100.0% |
| Findings without citation | 0 |

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

- **F3** (unmatched) top score 0.15 
  > - A 10% discount is applied to orders above 50 units (calc.cob:L12-L13).
- **F4** (unmatched) top score 0.17 invalid cits: calc.cob:L40-L45
  > - The order total is written to the ORDERS file for audit (calc.cob:L40-L45).
- **F6** (unmatched) top score 0.00 
  > - The program terminates with STOP RUN after one calculation (L16).

## Invalid citations

- F4: `calc.cob:L40-L45` -> out of range (file has 16 lines)
