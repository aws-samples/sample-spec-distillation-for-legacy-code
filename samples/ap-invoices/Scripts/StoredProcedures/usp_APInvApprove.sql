-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_APInvApprove
--   @Action  'A' approve   (Pending Approval -> Approved)
--            'R' reject    (Pending Approval -> Open, reason required)
--            'S' system    (already Approved by usp_APInvPost; holds only)
--
--   After approval the payment-compliance holds are applied:
--     LW  - job invoice >= APCoParm.LienWvrThreshold and no lien waiver
--     INS - vendor GL or WC certificate expired / missing expiry
--   If any hold goes on, status -> H and return 1.
--
--   return codes 0 ok, 1 approved+held, 2 bad status, 3 not authorized,
--                4 over limit, 5 reason required
--
--   KLT 2009-11 created
--   PSV 2013-06 compliance holds
-- =============================================================================
IF OBJECT_ID('dbo.usp_APInvApprove') IS NOT NULL DROP PROCEDURE dbo.usp_APInvApprove
GO
CREATE PROCEDURE dbo.usp_APInvApprove
    @APCo     tinyint,
    @Mth      smalldatetime,
    @APTrans  int,
    @Action   char(1),
    @Reason   varchar(255) = NULL,
    @UserId   varchar(30),
    @msg      varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @rcode int, @Status char(1), @GrossAmt numeric(12,2), @EntBy varchar(30),
        @Role varchar(5), @MaxAmt numeric(12,2), @Job varchar(10), @LienWvrRcvd char(1),
        @VendGrp tinyint, @Vendor int, @LWThr numeric(12,2), @held int

SELECT @rcode = 0, @held = 0

SELECT @Status = Status, @GrossAmt = GrossAmt, @EntBy = EntBy, @Job = Job,
       @LienWvrRcvd = LienWvrRcvd, @VendGrp = VendGrp, @Vendor = Vendor
  FROM dbo.APInvHdr
 WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'Invoice not found.', @rcode = 2
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- reject
-- ---------------------------------------------------------------------------
IF @Action = 'R'
BEGIN
    IF @Status <> 'P'
    BEGIN
        SELECT @msg = 'Only Pending Approval invoices can be rejected.', @rcode = 2
        GOTO bspexit
    END
    IF LEN(LTRIM(RTRIM(ISNULL(@Reason, '')))) = 0
    BEGIN
        SELECT @msg = 'Rejection reason is required.', @rcode = 5
        GOTO bspexit
    END
    UPDATE dbo.APInvHdr
       SET Status = 'O', PostedYN = 'N', RejReason = @Reason, ApprBy = NULL, ApprDate = NULL
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- approve
-- ---------------------------------------------------------------------------
IF @Action = 'A'
BEGIN
    IF @Status <> 'P'
    BEGIN
        SELECT @msg = 'Invoice is not Pending Approval.', @rcode = 2
        GOTO bspexit
    END

    -- segregation of duties
    IF @EntBy = @UserId
    BEGIN
        SELECT @msg = 'You cannot approve an invoice you entered.', @rcode = 3
        GOTO bspexit
    END

    SELECT @Role = APRole FROM dbo.SecUser WHERE UserId = @UserId AND ActiveYN = 'Y'
    SELECT @MaxAmt = MaxAmt FROM dbo.APApprLim WHERE APRole = @Role
    IF @MaxAmt IS NULL OR @MaxAmt <= 0
    BEGIN
        SELECT @msg = 'User ' + @UserId + ' has no approval authority.', @rcode = 3
        GOTO bspexit
    END
    IF @GrossAmt > @MaxAmt
    BEGIN
        SELECT @msg = 'Invoice exceeds approval limit for role ' + @Role + '.', @rcode = 4
        GOTO bspexit
    END

    UPDATE dbo.APInvHdr
       SET Status = 'A', ApprBy = @UserId, ApprDate = GETDATE()
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
END
ELSE IF @Action = 'S'
BEGIN
    IF @Status <> 'A'
    BEGIN
        SELECT @msg = 'System approval expects an Approved invoice.', @rcode = 2
        GOTO bspexit
    END
    UPDATE dbo.APInvHdr SET ApprBy = 'SYSTEM', ApprDate = GETDATE()
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
END
ELSE
BEGIN
    SELECT @msg = 'Invalid action.', @rcode = 2
    GOTO bspexit
END

-- ---------------------------------------------------------------------------
-- compliance holds
-- ---------------------------------------------------------------------------
SELECT @LWThr = LienWvrThreshold FROM dbo.APCoParm WHERE APCo = @APCo

IF @Job IS NOT NULL AND @GrossAmt >= ISNULL(@LWThr, 5000) AND @LienWvrRcvd <> 'Y'
BEGIN
    INSERT dbo.APHold (APCo, Mth, APTrans, HoldCode, HoldDate, HoldBy, Notes)
    VALUES (@APCo, @Mth, @APTrans, 'LW', GETDATE(), 'SYSTEM', 'Lien waiver required before payment')
    SELECT @held = @held + 1
END

IF EXISTS (SELECT 1 FROM dbo.APVendComp
            WHERE VendGrp = @VendGrp AND Vendor = @Vendor AND CompCode IN ('GL', 'WC')
              AND (ExpDate IS NULL OR ExpDate < CONVERT(smalldatetime, CONVERT(varchar(10), GETDATE(), 112))))
BEGIN
    INSERT dbo.APHold (APCo, Mth, APTrans, HoldCode, HoldDate, HoldBy, Notes)
    VALUES (@APCo, @Mth, @APTrans, 'INS', DATEADD(ms, 3, GETDATE()), 'SYSTEM', 'Vendor insurance expired')
    SELECT @held = @held + 1
END

IF @held > 0
BEGIN
    UPDATE dbo.APInvHdr SET Status = 'H'
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
    SELECT @msg = CONVERT(varchar(3), @held) + ' compliance hold(s) applied.', @rcode = 1
END

bspexit:
RETURN @rcode
GO
