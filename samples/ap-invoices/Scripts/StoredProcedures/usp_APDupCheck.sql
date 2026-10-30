-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_APDupCheck
--   Returns possible duplicate invoices for a vendor.
--   Called from the entry screen (warning) and from usp_APInvPost (block).
--   KLT 2009-11 created
--   KLT 2010-02 exclude voided
--   RDM 2016-02 window from APCoParm.DupDays (was hardcoded 60)
-- =============================================================================
IF OBJECT_ID('dbo.usp_APDupCheck') IS NOT NULL DROP PROCEDURE dbo.usp_APDupCheck
GO
CREATE PROCEDURE dbo.usp_APDupCheck
    @APCo       tinyint,
    @VendGrp    tinyint,
    @Vendor     int,
    @APRef      varchar(15),
    @GrossAmt   numeric(12,2),
    @InvDate    smalldatetime,
    @ExclMth    smalldatetime = NULL,   -- exclude self when re-checking a saved invoice
    @ExclTrans  int = NULL
AS
SET NOCOUNT ON

DECLARE @DupDays smallint

SELECT @DupDays = DupDays FROM dbo.APCoParm WHERE APCo = @APCo
IF @DupDays IS NULL SELECT @DupDays = 90

-- credit memos are not dup checked
IF @GrossAmt < 0
BEGIN
    SELECT TOP 0 Mth, APTrans, APRef, InvDate, GrossAmt, Status FROM dbo.APInvHdr
    RETURN 0
END

SELECT h.Mth, h.APTrans, h.APRef, h.InvDate, h.GrossAmt, h.Status
FROM dbo.APInvHdr h
WHERE h.APCo    = @APCo
  AND h.VendGrp = @VendGrp
  AND h.Vendor  = @Vendor
  AND UPPER(LTRIM(RTRIM(h.APRef))) = UPPER(LTRIM(RTRIM(@APRef)))
  AND h.GrossAmt = @GrossAmt
  AND ABS(DATEDIFF(day, h.InvDate, @InvDate)) <= @DupDays
  AND h.Status <> 'V'
  AND NOT (h.Mth = ISNULL(@ExclMth, '19000101') AND h.APTrans = ISNULL(@ExclTrans, -1))
ORDER BY h.InvDate DESC

RETURN 0
GO
