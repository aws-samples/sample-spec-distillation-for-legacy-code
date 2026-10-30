-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- usp_JCCOFieldImport
--   Creates a Draft change order from a field app request (clsJCFieldSync).
--   Items come in as <items><i ph ct u um uc tx/></items>.
--   Same extRef twice returns the CO already created for it.
--   Field COs use the company default markup.
--
--   return codes 0 created, 1 duplicate extRef (@CONum = existing CO),
--                2 job / subcontract not found, 3 job closed,
--                4 reason code not valid, 5 over field limit
--   TKB 2014-09
-- =============================================================================
IF OBJECT_ID('dbo.usp_JCCOFieldImport') IS NOT NULL DROP PROCEDURE dbo.usp_JCCOFieldImport
GO
CREATE PROCEDURE dbo.usp_JCCOFieldImport
    @JCCo        tinyint,
    @Job         varchar(10),
    @ExtRef      varchar(36),
    @Description varchar(60),
    @COType      char(1),
    @ReasonCode  varchar(3),
    @SL          varchar(12) = NULL,
    @ReqDate     smalldatetime,
    @ReqBy       varchar(30),
    @DaysExt     smallint = 0,
    @ItemsXml    xml,
    @CONum       varchar(3) = NULL OUTPUT,
    @msg         varchar(255) = NULL OUTPUT
AS
SET NOCOUNT ON

DECLARE @JobStatus tinyint, @Pct numeric(6,3), @FldMax numeric(12,2), @Cost numeric(12,2), @rc int

SELECT @CONum = CONum FROM dbo.JCCOHdr WHERE ExtRef = @ExtRef
IF @CONum IS NOT NULL
BEGIN
    SELECT @msg = 'Already received.'
    RETURN 1
END

SELECT @JobStatus = JobStatus FROM dbo.JCJob WHERE JCCo = @JCCo AND Job = @Job
IF @JobStatus IS NULL
BEGIN
    SELECT @msg = 'Job ' + @Job + ' not found.'
    RETURN 2
END
IF @JobStatus = 3
BEGIN
    SELECT @msg = 'Job ' + @Job + ' is closed.'
    RETURN 3
END

IF @COType = 'S' AND NOT EXISTS (SELECT 1 FROM dbo.SLHdr WHERE SLCo = @JCCo AND SL = @SL AND Job = @Job)
BEGIN
    SELECT @msg = 'Subcontract ' + ISNULL(@SL, '') + ' not found on job ' + @Job + '.'
    RETURN 2
END

IF NOT EXISTS (SELECT 1 FROM dbo.JCCOReason WHERE ReasonCode = @ReasonCode AND ActiveYN = 'Y')
BEGIN
    SELECT @msg = 'Reason code ' + ISNULL(@ReasonCode, '') + ' is not valid.'
    RETURN 4
END

SELECT @Pct = DfltMarkupPct, @FldMax = FieldMaxAmt FROM dbo.JCCoParm WHERE JCCo = @JCCo

SELECT @Cost = ISNULL(SUM(ROUND(x.i.value('@u', 'numeric(12,3)') * x.i.value('@uc', 'numeric(12,4)'), 2)), 0)
  FROM @ItemsXml.nodes('/items/i') x(i)
IF @Cost > @FldMax
BEGIN
    SELECT @msg = 'Over field request limit.'
    RETURN 5
END

BEGIN TRAN

EXEC @rc = dbo.usp_JCCONextNum @JCCo, @Job, @CONum OUTPUT, @msg OUTPUT
IF @rc <> 0
BEGIN
    ROLLBACK TRAN
    RETURN 2
END

INSERT dbo.JCCOHdr (JCCo, Job, CONum, Description, COType, ReasonCode, Status, SL, ReqDate, ReqBy,
                    EntBy, MarkupPct, DaysExt, SourceSys, ExtRef)
VALUES (@JCCo, @Job, @CONum, @Description, @COType, @ReasonCode, 'D',
        CASE WHEN @COType = 'S' THEN @SL ELSE NULL END, @ReqDate, @ReqBy,
        'FIELDAPI', @Pct, @DaysExt, 'FLD', @ExtRef)

INSERT dbo.JCCOItem (JCCo, Job, CONum, Item, Phase, CostType, Units, UM, UnitCost, Amount, TaxYN)
SELECT @JCCo, @Job, @CONum,
       ROW_NUMBER() OVER (ORDER BY (SELECT 1)),
       x.i.value('@ph', 'varchar(20)'),
       x.i.value('@ct', 'char(1)'),
       x.i.value('@u', 'numeric(12,3)'),
       x.i.value('@um', 'varchar(3)'),
       x.i.value('@uc', 'numeric(12,4)'),
       ROUND(x.i.value('@u', 'numeric(12,3)') * x.i.value('@uc', 'numeric(12,4)'), 2),
       x.i.value('@tx', 'char(1)')
  FROM @ItemsXml.nodes('/items/i') x(i)

IF @@ERROR <> 0
BEGIN
    ROLLBACK TRAN
    SELECT @msg = 'Insert failed.', @CONum = NULL
    RETURN 99
END

COMMIT TRAN
SELECT @msg = 'Received.'
RETURN 0
GO
