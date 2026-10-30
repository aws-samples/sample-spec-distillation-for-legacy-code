-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_APRetgCompute
--   @Mode 'C' = (re)compute retainage on lines + header, put RET hold on
--   @Mode 'R' = release retainage (job must be complete). Called by
--               usp_APHoldRelease when the RET hold is released.
--
--   Retainage is withheld from the payable until the job is complete. The
--   RET hold does NOT stop payment of the non-retained portion (see trigger).
--
--   RDM 2004-03 created
--   RDM 2016-02 per line calc, ROUND to cent. header = sum of lines (bug 2231)
--   PSV 2017-04 release mode
-- =============================================================================
IF OBJECT_ID('dbo.usp_APRetgCompute') IS NOT NULL DROP PROCEDURE dbo.usp_APRetgCompute
GO
CREATE PROCEDURE dbo.usp_APRetgCompute
    @APCo     tinyint,
    @Mth      smalldatetime,
    @APTrans  int,
    @Mode     char(1) = 'C',
    @UserId   varchar(30) = NULL,
    @msg      varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @Job varchar(10), @JobStatus tinyint, @RetgAmt numeric(12,2), @rcode int
SELECT @rcode = 0, @UserId = ISNULL(@UserId, SUSER_SNAME())

SELECT @Job = Job FROM dbo.APInvHdr WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Invoice not found.', @rcode = 1
    GOTO bspexit
END

IF @Mode = 'C'
BEGIN
    -- job lines only, on Amount (tax is never retained)
    UPDATE dbo.APInvLine
       SET RetgAmt = CASE WHEN LineType = 1 AND RetgPct > 0
                          THEN ROUND(Amount * RetgPct / 100.0, 2)
                          ELSE 0 END,
           RetgPct = CASE WHEN LineType = 1 THEN RetgPct ELSE 0 END
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans

    SELECT @RetgAmt = ISNULL(SUM(RetgAmt), 0)
      FROM dbo.APInvLine
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans

    UPDATE dbo.APInvHdr SET RetgAmt = @RetgAmt
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans

    IF @RetgAmt > 0 AND NOT EXISTS (SELECT 1 FROM dbo.APHold
                                     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
                                       AND HoldCode = 'RET' AND RelDate IS NULL)
    BEGIN
        INSERT dbo.APHold (APCo, Mth, APTrans, HoldCode, HoldDate, HoldBy, Notes)
        VALUES (@APCo, @Mth, @APTrans, 'RET', GETDATE(), 'SYSTEM', 'Retainage withheld until job complete')
    END
    GOTO bspexit
END

IF @Mode = 'R'
BEGIN
    SELECT @JobStatus = JobStatus FROM dbo.JCJob WHERE JCCo = @APCo AND Job = @Job
    -- 1 open, 2 complete, 3 closed. retainage only comes out once the job is complete
    IF ISNULL(@JobStatus, 1) < 2
    BEGIN
        SELECT @msg = 'Job ' + ISNULL(@Job, '?') + ' is not complete. Retainage cannot be released.', @rcode = 20
        GOTO bspexit
    END

    UPDATE dbo.APInvHdr SET RetgRelAmt = RetgAmt
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
    GOTO bspexit
END

SELECT @msg = 'Invalid mode ' + ISNULL(@Mode, 'NULL'), @rcode = 1

bspexit:
RETURN @rcode
GO
