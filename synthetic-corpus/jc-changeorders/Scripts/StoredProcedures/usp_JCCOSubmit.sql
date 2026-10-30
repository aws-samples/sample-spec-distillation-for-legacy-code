-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_JCCOSubmit
--   Draft -> Pending Approval.
--   Recomputes item amounts and the header totals from the items (the screen
--   totals are only a preview), then checks the CO and submits it.
--     Cost   = sum(item Amount)              Amount = ROUND(Units * UnitCost, 2)
--     Markup = ROUND(Cost * MarkupPct / 100, 2)
--     Tax    = ROUND(taxable Cost * JCJob.TaxRate, 2)
--     Total  = Cost + Markup + Tax
--
--   return codes 0 ok, 1 not found, 2 bad status, 6 job closed, 7 no items,
--                8 markup over max, 9 VAL must be a deduct,
--                10 sub CO past notice period
--
--   DWH 2006-08 created
--   MJR 2010-02 sub CO notice period
--   MJR 2013-01 T&M owner COs
-- =============================================================================
IF OBJECT_ID('dbo.usp_JCCOSubmit') IS NOT NULL DROP PROCEDURE dbo.usp_JCCOSubmit
GO
CREATE PROCEDURE dbo.usp_JCCOSubmit
    @JCCo    tinyint,
    @Job     varchar(10),
    @CONum   varchar(3),
    @UserId  varchar(30),
    @msg     varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @Status char(1), @COType char(1), @Reason varchar(3), @ReqDate smalldatetime,
        @Pct numeric(6,3), @MaxPct numeric(6,3), @NoticeDays smallint,
        @JobStatus tinyint, @CT varchar(3), @TaxRate numeric(8,6),
        @Cost numeric(12,2), @Taxable numeric(12,2), @Mk numeric(12,2), @Tax numeric(12,2),
        @Tot numeric(12,2), @NewStat char(1)

SELECT @Status = Status, @COType = COType, @Reason = ReasonCode, @ReqDate = ReqDate, @Pct = MarkupPct
  FROM dbo.JCCOHdr
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Change order not found.'
    RETURN 1
END

IF @Status <> 'D'
BEGIN
    SELECT @msg = 'Only draft change orders can be submitted.'
    RETURN 2
END

SELECT @JobStatus = JobStatus, @CT = ContractType, @TaxRate = TaxRate
  FROM dbo.JCJob WHERE JCCo = @JCCo AND Job = @Job

IF @JobStatus = 3
BEGIN
    SELECT @msg = 'Job ' + @Job + ' is closed.'
    RETURN 6
END

IF NOT EXISTS (SELECT 1 FROM dbo.JCCOItem WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum)
BEGIN
    SELECT @msg = 'Change order has no items.'
    RETURN 7
END

SELECT @MaxPct = MaxMarkupPct, @NoticeDays = NoticeDays FROM dbo.JCCoParm WHERE JCCo = @JCCo

IF @Pct > @MaxPct
BEGIN
    SELECT @msg = 'Markup ' + CONVERT(varchar(10), @Pct) + '% is over the company maximum of '
                + CONVERT(varchar(10), @MaxPct) + '%.'
    RETURN 8
END

-- ---------------------------------------------------------------------------
-- amounts
-- ---------------------------------------------------------------------------
UPDATE dbo.JCCOItem SET Amount = ROUND(Units * UnitCost, 2)
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum

SELECT @Cost    = ISNULL(SUM(Amount), 0),
       @Taxable = ISNULL(SUM(CASE WHEN TaxYN = 'Y' THEN Amount ELSE 0 END), 0)
  FROM dbo.JCCOItem
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum

IF @COType = 'S' AND @Pct > 5.000
    SELECT @Pct = 5.000

SELECT @Mk  = ROUND(@Cost * @Pct / 100.0, 2)
SELECT @Tax = ROUND(@Taxable * @TaxRate, 2)
SELECT @Tot = @Cost + @Mk + @Tax

IF @Reason = 'VAL' AND @Tot >= 0
BEGIN
    SELECT @msg = 'Value engineering change orders must be a deduct.'
    RETURN 9
END

IF @COType = 'S' AND DATEDIFF(day, @ReqDate, GETDATE()) > @NoticeDays
BEGIN
    SELECT @msg = 'Subcontract change orders must be submitted within '
                + CONVERT(varchar(5), @NoticeDays) + ' days of the request date.'
    RETURN 10
END

-- ---------------------------------------------------------------------------
-- submit
-- ---------------------------------------------------------------------------
SELECT @NewStat = 'P'
IF @CT = 'TM' AND @COType = 'O'
    SELECT @NewStat = 'A'

UPDATE dbo.JCCOHdr
   SET CostAmt    = @Cost,
       MarkupAmt  = @Mk,
       TaxAmt     = @Tax,
       TotalAmt   = @Tot,
       Status     = @NewStat,
       SubmitDate = GETDATE(),
       ApprBy     = CASE WHEN @NewStat = 'A' THEN 'SYSTEM' ELSE NULL END,
       ApprDate   = CASE WHEN @NewStat = 'A' THEN GETDATE() ELSE NULL END
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum

SELECT @msg = 'Submitted.'
RETURN 0
GO
