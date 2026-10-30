-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_JCCONextNum
--   next change order number for a job. 3 chars, zero padded, 001-999.
--   Numbers are never reused (void COs keep their number).
--   return codes 0 ok, 1 job not found, 2 job is out of CO numbers
--   DWH 2006-08
-- =============================================================================
IF OBJECT_ID('dbo.usp_JCCONextNum') IS NOT NULL DROP PROCEDURE dbo.usp_JCCONextNum
GO
CREATE PROCEDURE dbo.usp_JCCONextNum
    @JCCo   tinyint,
    @Job    varchar(10),
    @CONum  varchar(3) = NULL OUTPUT,
    @msg    varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @n int

IF NOT EXISTS (SELECT 1 FROM dbo.JCJob WHERE JCCo = @JCCo AND Job = @Job)
BEGIN
    SELECT @msg = 'Job not found.'
    RETURN 1
END

SELECT @n = ISNULL(MAX(CAST(CONum AS int)), 0) + 1
  FROM dbo.JCCOHdr WITH (UPDLOCK, HOLDLOCK)
 WHERE JCCo = @JCCo AND Job = @Job

IF @n > 999
BEGIN
    SELECT @msg = 'Job ' + @Job + ' has used all 999 change order numbers.'
    RETURN 2
END

SELECT @CONum = RIGHT('000' + CONVERT(varchar(3), @n), 3)
RETURN 0
GO
