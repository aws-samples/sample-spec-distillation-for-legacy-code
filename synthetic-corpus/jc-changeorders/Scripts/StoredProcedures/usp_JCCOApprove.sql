-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_JCCOApprove
--   @Action  'A' approve   (Pending Approval -> Approved)
--            'R' reject    (Pending Approval -> Rejected, reason required)
--
--   Approve checks:
--     approver has a JC role above ENTRY and did not enter the CO
--     PM can approve up to JCCoParm.COApprThreshold
--     amount within the role limit in JCCOApprLim
--     sub COs: subcontract open, insurance current, CO cap not exceeded
--
--   return codes 0 ok, 1 not found, 2 bad status, 3 not authorized,
--                4 over limit, 5 reason required, 11 subcontract not open,
--                12 sub insurance expired, 13 over subcontract CO cap
--
--   MJR 2010-02 created
--   MJR 2011-06 sub insurance
--   TKB 2018-05 support override
-- =============================================================================
IF OBJECT_ID('dbo.usp_JCCOApprove') IS NOT NULL DROP PROCEDURE dbo.usp_JCCOApprove
GO
CREATE PROCEDURE dbo.usp_JCCOApprove
    @JCCo     tinyint,
    @Job      varchar(10),
    @CONum    varchar(3),
    @Action   char(1),
    @Reason   varchar(255) = NULL,
    @UserId   varchar(30),
    @Override char(1) = 'N',
    @msg      varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @Status char(1), @COType char(1), @EntBy varchar(30), @Tot numeric(12,2),
        @Cost numeric(12,2), @SL varchar(12), @Role varchar(5), @MaxAmt numeric(14,2),
        @Thr numeric(12,2), @DfltCOPct numeric(6,3), @SLStat tinyint, @InsExp smalldatetime,
        @Orig numeric(14,2), @SLPct numeric(6,3), @Used numeric(14,2), @Ovr char(1)

SELECT @Status = Status, @COType = COType, @EntBy = EntBy, @Tot = TotalAmt, @Cost = CostAmt, @SL = SL
  FROM dbo.JCCOHdr
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Change order not found.'
    RETURN 1
END

IF @Status <> 'P'
BEGIN
    SELECT @msg = 'Change order is not pending approval.'
    RETURN 2
END

-- ---------------------------------------------------------------------------
-- reject
-- ---------------------------------------------------------------------------
IF @Action = 'R'
BEGIN
    IF LEN(LTRIM(RTRIM(ISNULL(@Reason, '')))) = 0
    BEGIN
        SELECT @msg = 'Rejection reason is required.'
        RETURN 5
    END
    UPDATE dbo.JCCOHdr
       SET Status = 'R', RejReason = @Reason, ApprBy = NULL, ApprDate = NULL
     WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum
    SELECT @msg = 'Rejected.'
    RETURN 0
END

-- ---------------------------------------------------------------------------
-- approve
-- ---------------------------------------------------------------------------
SELECT @Role = JCRole FROM dbo.SecUser WHERE UserName = @UserId AND ActiveYN = 'Y'
IF ISNULL(@Role, 'ENTRY') = 'ENTRY'
BEGIN
    SELECT @msg = 'User has no change order approval authority.'
    RETURN 3
END

IF @EntBy = @UserId
BEGIN
    SELECT @msg = 'You cannot approve a change order you entered.'
    RETURN 3
END

SELECT @Ovr = 'N'
IF @Override = 'Y' AND EXISTS (SELECT 1 FROM dbo.JCAdmUsers WHERE UserName = @UserId)
    SELECT @Ovr = 'Y'

SELECT @Thr = COApprThreshold, @DfltCOPct = DfltMaxCOPct FROM dbo.JCCoParm WHERE JCCo = @JCCo
SELECT @MaxAmt = MaxAmt FROM dbo.JCCOApprLim WHERE JCCo = @JCCo AND Role = @Role

IF @Ovr = 'N'
BEGIN
    IF @Role = 'PM' AND @Tot > @Thr
    BEGIN
        SELECT @msg = 'Change orders over ' + CONVERT(varchar(20), @Thr) + ' need executive approval.'
        RETURN 4
    END
    IF @Tot > ISNULL(@MaxAmt, 0)
    BEGIN
        SELECT @msg = 'Amount is over the approval limit for role ' + @Role + '.'
        RETURN 4
    END
END

-- ---------------------------------------------------------------------------
-- subcontract checks
-- ---------------------------------------------------------------------------
IF @COType = 'S'
BEGIN
    SELECT @SLStat = SLStatus, @InsExp = InsExpDate, @Orig = OrigCost, @SLPct = ISNULL(MaxCOPct, @DfltCOPct)
      FROM dbo.SLHdr WHERE SLCo = @JCCo AND SL = @SL

    IF ISNULL(@SLStat, 2) <> 0
    BEGIN
        SELECT @msg = 'Subcontract ' + ISNULL(@SL, '?') + ' is not open.'
        RETURN 11
    END

    IF @InsExp IS NULL OR @InsExp < GETDATE()
    BEGIN
        SELECT @msg = 'Subcontractor insurance is expired or missing.'
        RETURN 12
    END

    SELECT @Used = ISNULL(SUM(CostAmt), 0)
      FROM dbo.JCCOHdr
     WHERE JCCo = @JCCo AND SL = @SL AND COType = 'S' AND Status IN ('A', 'I')

    IF @Used + @Cost > ROUND(@Orig * @SLPct / 100.0, 2)
    BEGIN
        SELECT @msg = 'Change order would exceed the subcontract change order limit.'
        RETURN 13
    END
END

UPDATE dbo.JCCOHdr
   SET Status = 'A', ApprBy = @UserId, ApprDate = GETDATE(), OvrFlag = @Ovr
 WHERE JCCo = @JCCo AND Job = @Job AND CONum = @CONum

SELECT @msg = 'Approved.'
RETURN 0
GO
