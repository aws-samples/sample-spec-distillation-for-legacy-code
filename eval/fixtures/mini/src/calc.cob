       IDENTIFICATION DIVISION.
       PROGRAM-ID. Calc.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  QTY        PIC 9(3) VALUE 0.
       01  PRICE      PIC 9(4)V99 VALUE 12.50.
       01  TOTAL-AMT  PIC 9(6)V99.
       PROCEDURE DIVISION.
           ACCEPT QTY
           IF QTY > 100
               DISPLAY "Quantity too large"
           ELSE
               MULTIPLY QTY BY PRICE GIVING TOTAL-AMT
               DISPLAY "Total: " TOTAL-AMT
           END-IF
           STOP RUN.
