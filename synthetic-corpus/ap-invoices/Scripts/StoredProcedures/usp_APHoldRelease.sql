-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_APHoldRelease
--   Releases one open hold on an invoice. Who may release is in
--   APHoldCode.RelRole; CTRL and CFO may release anything EXCEPT INS.
--   When the last blocking hold comes off the invoice goes back to Approved.
--
--   return codes 0 ok, 1 not found, 3 not authorized, 6 still non-compliant,
--                20 (from usp_APRetgCompute) job not complete
--
--   PSV 2013-06 created
--   PSV 2017-04 RET release
-- =============================================================================
IF OBJECT_ID('dbo.usp_APHoldRelease') IS NOT NULL DROP PROCEDURE dbo.usp_APHoldRelease
GO
CREATE PROCEDURE dbo.usp_APHoldRelease
    @APCo      tinyint,
    @Mth       smalldatetime,
    @APTrans   int,
    @HoldCode  varchar(4),
    @UserId    varchar(30),
    @Notes     varchar(255) = NULL,
    @msg       varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @rcode int, @Role varchar(5), @RelRole varchar(5), @HoldBy varchar(30),
        @HoldDate datetime, @VendGrp tinyint, @Vendor int, @ok bit, @Status char(1)

SELECT @rcode = 0, @ok = 0

SELECT TOP 1 @HoldBy = h.HoldBy, @HoldDate = h.HoldDate, @RelRole = c.RelRole
  FROM dbo.APHold h JOIN dbo.APHoldCode c ON c.HoldCode = h.HoldCode
 WHERE h.APCo = @APCo AND h.Mth = @Mth AND h.APTrans = @APTrans
   AND h.HoldCode = @HoldCode AND h.RelDate IS NULL
 ORDER BY h.HoldDate
IF @@ROWCOUNT = 0
BEGIN
    SELECT @msg = 'No open ' + @HoldCode + ' hold on this invoice.', @rcode = 1
    GOTO bspexit
END

SELECT @Role = APRole FROM dbo.SecUser WHERE UserId = @UserId AND ActiveYN = 'Y'

-- ---------------------------------------------------------------------------
-- authority
-- ---------------------------------------------------------------------------
IF @HoldCode = 'INS'
BEGIN
    IF @Role = 'RISK' SELECT @ok = 1        -- risk mgmt only. no override.
END
ELSE IF @HoldCode = 'MGR' AND @HoldBy = @UserId
    SELECT @ok = 1
ELSE IF @Role IN ('CTRL', 'CFO') OR @Role = @RelRole
    SELECT @ok = 1

IF @ok = 0
BEGIN
    SELECT @msg = 'User ' + @UserId + ' (' + ISNULL(@Role, 'none') + ') may not release ' + @HoldCode + ' holds.',
           @rcode = 3
    GOTO bspexit
END

SELECT @VendGrp = VendGrp, @Vendor = Vendor, @Status = Status
  FROM dbo.APInvHdr WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans

-- ---------------------------------------------------------------------------
-- hold specific
-- ---------------------------------------------------------------------------
IF @HoldCode = 'INS'
BEGIN
    -- can't release while a cert is still expired; fix the cert first
    IF EXISTS (SELECT 1 FROM dbo.APVendComp
                WHERE VendGrp = @VendGrp AND Vendor = @Vendor AND CompCode IN ('GL', 'WC')
                  AND (ExpDate IS NULL OR ExpDate < CONVERT(smalldatetime, CONVERT(varchar(10), GETDATE(), 112))))
    BEGIN
        SELECT @msg = 'Vendor insurance is still expired. Update APVendComp first.', @rcode = 6
        GOTO bspexit
    END
END

IF @HoldCode = 'LW'
BEGIN
    UPDATE dbo.APInvHdr SET LienWvrRcvd = 'Y'
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
END

IF @HoldCode = 'RET'
BEGIN
    EXEC @rcode = dbo.usp_APRetgCompute @APCo, @Mth, @APTrans, 'R', @UserId, @msg OUTPUT
    IF @rcode <> 0 GOTO bspexit
END

UPDATE dbo.APHold
   SET RelDate = GETDATE(), RelBy = @UserId,
       Notes = ISNULL(Notes, '') + CASE WHEN @Notes IS NULL THEN '' ELSE ' | ' + @Notes END
 WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans AND HoldCode = @HoldCode AND HoldDate = @HoldDate

-- ---------------------------------------------------------------------------
-- back to Approved when no blocking holds remain (RET does not block)
-- ---------------------------------------------------------------------------
IF @Status = 'H' AND NOT EXISTS (SELECT 1 FROM dbo.APHold
                                  WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
                                    AND RelDate IS NULL AND HoldCode <> 'RET')
BEGIN
    UPDATE dbo.APInvHdr SET Status = 'A'
     WHERE APCo = @APCo AND Mth = @Mth AND APTrans = @APTrans
END

-- 2014: MGR holds older than APCoParm.MgrHoldDays were auto released here.
-- removed per audit finding, column left in APCoParm. (PSV)

bspexit:
RETURN @rcode
GO
