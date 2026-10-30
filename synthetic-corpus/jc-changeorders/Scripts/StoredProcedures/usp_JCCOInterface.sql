-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_JCCOInterface
--   Approved -> Interfaced. Posts an approved change order:
--     owner CO  : JCJob.RevContract += TotalAmt
--     sub CO    : SLHdr.CurCost += CostAmt, JCCommit rows by phase / cost type
--                 (AP matches sub invoices against JCCommit)
--     internal  : nothing posted, status only (budget moves are done in JC
--                 budget entry)
--   return codes 0 ok, 1 not found, 2 bad status, 6 job closed
--   DWH 2006-08, MJR 2010-02 committed cost
-- =============================================================================
IF OBJECT_ID('dbo.usp_JCCOInterface') IS NOT NULL DROP PROCEDURE dbo.usp_JCCOInterface
GO
CREATE PROCEDURE dbo.usp_JCCOInterface
    @JCCo    tinyint,
    @Job     varchar(10),
    @CONum   varchar(3),
    @UserId  varchar(30),
    @msg     varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @Status char(1), @COType char(1), @Tot numeric(12,2), @Cost numeric(12,2),
        @SL varchar(12), @JobStatus tinyint

SELECT @Status = Status, @COType = COType, @Tot = TotalAmt, @Cost = CostAmt, @SL = SL
  FROM dbo.JCCOHdr
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Change order not found.'
    RETURN 1
END

IF @Status <> 'A'
BEGIN
    SELECT @msg = 'Only approved change orders can be posted.'
    RETURN 2
END

SELECT @JobStatus = JobStatus FROM dbo.JCJob WHERE JCCo = @JCCo AND Job = @Job
IF @JobStatus = 3
BEGIN
    SELECT @msg = 'Job ' + @Job + ' is closed.'
    RETURN 6
END

BEGIN TRAN

IF @COType = 'O'
    UPDATE dbo.JCJob SET RevContract = RevContract + @Tot
     WHERE JCCo = @JCCo AND Job = @Job

IF @COType = 'S'
BEGIN
    UPDATE dbo.SLHdr SET CurCost = CurCost + @Cost
     WHERE SLCo = @JCCo AND SL = @SL

    INSERT dbo.JCCommit (JCCo, Job, Phase, CostType, SL, CONum, CmtdAmt, PostDate)
    SELECT JCCo, Job, Phase, CostType, @SL, CONum, SUM(Amount), GETDATE()
      FROM dbo.JCCOItem
     WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum
     GROUP BY JCCo, Job, Phase, CostType, CONum
END

UPDATE dbo.JCCOHdr SET Status = 'I'
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum

IF @@ERROR <> 0
BEGIN
    ROLLBACK TRAN
    SELECT @msg = 'Post failed.'
    RETURN 99
END

COMMIT TRAN
SELECT @msg = 'Posted.'
RETURN 0
GO
