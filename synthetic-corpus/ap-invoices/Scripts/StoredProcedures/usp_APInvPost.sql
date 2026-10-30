-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_APInvPost
--   Posts an entered (Open) invoice. Validates, computes retainage and routes
--   to Pending Approval or straight to Approved.
--
--   return codes
--     0  ok                      10 GL month closed
--     1  not found / bad status  11 vendor inactive
--     12 1099 vendor, no TIN     13 lines out of balance
--     14 duplicate               15 dup override not authorized
--
--   RDM 2004-03 created
--   KLT 2009-11 dup check, approval routing
--   PSV 2013-06 auto-approve path calls usp_APInvApprove 'S' for holds
--   MJB 2018-01 1099 TIN check (IRS B-notice)
-- =============================================================================
IF OBJECT_ID('dbo.usp_APInvPost') IS NOT NULL DROP PROCEDURE dbo.usp_APInvPost
GO
CREATE PROCEDURE dbo.usp_APInvPost
    @APCo       tinyint,
    @Mth        smalldatetime,
    @APTrans    int,
    @DupOvrdBy  varchar(30) = NULL,
    @msg        varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @rcode int, @GLCo tinyint, @Status char(1), @PostedYN char(1),
        @VendGrp tinyint, @Vendor int, @APRef varchar(15), @InvDate smalldatetime,
        @GrossAmt numeric(12,2), @LineTot numeric(12,2), @ApprThreshold numeric(12,2),
        @ActiveYN char(1), @V1099YN char(1), @TIN varchar(11), @OvrdRole varchar(5),
        @NewStatus char(1), @dupcnt int

SELECT @rcode = 0

SELECT @Status = Status, @PostedYN = PostedYN, @VendGrp = VendGrp, @Vendor = Vendor,
       @APRef = APRef, @InvDate = InvDate, @GrossAmt = GrossAmt
  FROM dbo.APInvHdr
 WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Invoice not found.', @rcode = 1
    GOTO bspexit
END
IF @Status <> 'O' OR @PostedYN = 'Y'
BEGIN
    SELECT @msg = 'Only Open, unposted invoices can be posted.', @rcode = 1
    GOTO bspexit
END

SELECT @GLCo = GLCo, @ApprThreshold = ApprThreshold FROM dbo.APCoParm WHERE APCo = @APCo

-- ---------------------------------------------------------------------------
-- closed GL month
-- ---------------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM dbo.GLMthStat WHERE GLCo = @GLCo AND Mth = @Mth AND Status = 'C')
BEGIN
    SELECT @msg = 'GL month ' + CONVERT(varchar(7), @Mth, 120) + ' is closed. Post to an open month.', @rcode = 10
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- vendor
-- ---------------------------------------------------------------------------
SELECT @ActiveYN = ActiveYN, @V1099YN = V1099YN, @TIN = TIN
  FROM dbo.APVend WHERE VendGrp = @VendGrp AND Vendor = @Vendor
IF ISNULL(@ActiveYN, 'N') <> 'Y'
BEGIN
    SELECT @msg = 'Vendor is inactive.', @rcode = 11
    GOTO bspexit
END
-- 1099 reportable vendors must have a TIN before anything posts to them
IF @V1099YN = 'Y' AND LEN(LTRIM(RTRIM(ISNULL(@TIN, '')))) = 0
BEGIN
    SELECT @msg = '1099 vendor has no TIN on file. Obtain W-9 before posting.', @rcode = 12
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- lines balance to header (Amount + TaxAmt)
-- ---------------------------------------------------------------------------
SELECT @LineTot = ISNULL(SUM(Amount + TaxAmt), 0)
  FROM dbo.APInvLine WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
IF @LineTot <> @GrossAmt
BEGIN
    SELECT @msg = 'Lines do not balance to invoice total.', @rcode = 13
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- duplicate check. screen warns, here we block unless overridden by CTRL/CFO
-- ---------------------------------------------------------------------------
CREATE TABLE #dup (Mth smalldatetime, APTrans int, APRef varchar(15), InvDate smalldatetime,
                   GrossAmt numeric(12,2), Status char(1))
INSERT #dup EXEC dbo.usp_APDupCheck @APCo, @VendGrp, @Vendor, @APRef, @GrossAmt, @InvDate, @Mth, @APTrans
SELECT @dupcnt = COUNT(*) FROM #dup
DROP TABLE #dup

IF @dupcnt > 0
BEGIN
    IF @DupOvrdBy IS NULL
    BEGIN
        SELECT @msg = 'Possible duplicate invoice for this vendor.', @rcode = 14
        GOTO bspexit
    END
    SELECT @OvrdRole = APRole FROM dbo.SecUser WHERE UserId = @DupOvrdBy AND ActiveYN = 'Y'
    IF ISNULL(@OvrdRole, '') NOT IN ('CTRL', 'CFO')
    BEGIN
        SELECT @msg = 'User ' + @DupOvrdBy + ' may not override duplicates.', @rcode = 15
        GOTO bspexit
    END
    UPDATE dbo.APInvHdr SET DupOvrdBy = @DupOvrdBy
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
END

-- ---------------------------------------------------------------------------
-- retainage
-- ---------------------------------------------------------------------------
EXEC @rcode = dbo.usp_APRetgCompute @APCo, @Mth, @APTrans, 'C', NULL, @msg OUTPUT
IF @rcode <> 0 GOTO bspexit

-- ---------------------------------------------------------------------------
-- route. over the threshold -> pending approval, else approve now
-- ---------------------------------------------------------------------------
IF @GrossAmt > ISNULL(@ApprThreshold, 10000)
    SELECT @NewStatus = 'P'
ELSE
    SELECT @NewStatus = 'A'

UPDATE dbo.APInvHdr
   SET Status = @NewStatus, PostedYN = 'Y'
 WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans

IF @NewStatus = 'A'
BEGIN
    -- system approval: no limit / SoD check, but auto holds still apply
    EXEC @rcode = dbo.usp_APInvApprove @APCo, @Mth, @APTrans, 'S', NULL, 'SYSTEM', @msg OUTPUT
    IF @rcode = 1 SELECT @rcode = 0     -- held is fine here
END

bspexit:
RETURN @rcode
GO
