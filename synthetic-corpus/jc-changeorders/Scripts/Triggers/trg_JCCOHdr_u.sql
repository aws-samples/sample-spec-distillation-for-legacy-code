-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- trg_JCCOHdr_u   update trigger on JCCOHdr
--   * guards status transitions
--   * void needs a reason
--   * approved / interfaced COs: amounts and type are frozen
--   * writes JCCOAudit on every status change
--   DWH 2006-08, MJR 2010-02, TKB 2014-09
-- =============================================================================
IF OBJECT_ID('dbo.trg_JCCOHdr_u') IS NOT NULL DROP TRIGGER dbo.trg_JCCOHdr_u
GO
CREATE TRIGGER dbo.trg_JCCOHdr_u ON dbo.JCCOHdr
FOR UPDATE
AS
IF @@ROWCOUNT = 0 RETURN
SET NOCOUNT ON

DECLARE @errmsg varchar(255)

-- ---------------------------------------------------------------------------
-- frozen once approved
-- ---------------------------------------------------------------------------
IF UPDATE(CostAmt) OR UPDATE(MarkupAmt) OR UPDATE(TaxAmt) OR UPDATE(TotalAmt) OR UPDATE(COType)
BEGIN
    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
                 ON d.JCCo = i.JCCo AND d.Job = i.Job AND d.CONum = i.CONum
                WHERE d.Status IN ('A', 'I')
                  AND (i.CostAmt <> d.CostAmt OR i.MarkupAmt <> d.MarkupAmt OR i.TaxAmt <> d.TaxAmt
                       OR i.TotalAmt <> d.TotalAmt OR i.COType <> d.COType))
    BEGIN
        SELECT @errmsg = 'Approved change order amounts cannot be changed.'
        GOTO error
    END
END

-- ---------------------------------------------------------------------------
-- status transitions
-- ---------------------------------------------------------------------------
IF UPDATE(Status)
BEGIN
    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
                 ON d.JCCo = i.JCCo AND d.Job = i.Job AND d.CONum = i.CONum
                WHERE i.Status <> d.Status
                  AND d.Status + '>' + i.Status NOT IN
                      ('D>P', 'D>A', 'D>V', 'P>A', 'P>R', 'R>D', 'R>V', 'A>I', 'A>V'))
    BEGIN
        SELECT @errmsg = 'Invalid change order status change.'
        GOTO error
    END

    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
                 ON d.JCCo = i.JCCo AND d.Job = i.Job AND d.CONum = i.CONum
                WHERE i.Status = 'V' AND d.Status <> 'V'
                  AND LEN(LTRIM(RTRIM(ISNULL(i.VoidReason, '')))) = 0)
    BEGIN
        SELECT @errmsg = 'Void reason is required.'
        GOTO error
    END

    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
                 ON d.JCCo = i.JCCo AND d.Job = i.Job AND d.CONum = i.CONum
                WHERE i.Status = 'V' AND d.Status = 'A' AND d.ExpFlag = 'Y')
    BEGIN
        SELECT @errmsg = 'Change order cannot be voided.'
        GOTO error
    END

    INSERT dbo.JCCOAudit (JCCo, Job, CONum, OldStatus, NewStatus, ChgBy, ChgDate, Note)
    SELECT i.JCCo, i.Job, i.CONum, d.Status, i.Status, SUSER_SNAME(), GETDATE(),
           CASE WHEN i.Status = 'R' THEN LEFT(i.RejReason, 255)
                WHEN i.Status = 'V' THEN LEFT(i.VoidReason, 255)
                WHEN i.OvrFlag = 'Y' THEN 'override'
                ELSE NULL END
      FROM inserted i JOIN deleted d
        ON d.JCCo = i.JCCo AND d.Job = i.Job AND d.CONum = i.CONum
     WHERE i.Status <> d.Status
END

RETURN

error:
    RAISERROR(@errmsg, 11, -1)
    ROLLBACK TRANSACTION
GO
