-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- trg_APInvHdr_u   update trigger on APInvHdr
--   * guards status transitions (keep in sync w/ clsAPInvoiceBL.IsTransitionAllowed)
--   * no paying an invoice with open blocking holds
--   * void needs a reason
--   * posted invoices: vendor / invoice # / amount are frozen
--   * writes APInvAudit on every status change
--   RDM 2004-03, KLT 2009-11, PSV 2013-06
-- =============================================================================
IF OBJECT_ID('dbo.trg_APInvHdr_u') IS NOT NULL DROP TRIGGER dbo.trg_APInvHdr_u
GO
CREATE TRIGGER dbo.trg_APInvHdr_u ON dbo.APInvHdr
FOR UPDATE
AS
IF @@ROWCOUNT = 0 RETURN
SET NOCOUNT ON

DECLARE @errmsg varchar(255)

-- ---------------------------------------------------------------------------
-- frozen fields once posted
-- ---------------------------------------------------------------------------
IF UPDATE(Vendor) OR UPDATE(APRef) OR UPDATE(GrossAmt) OR UPDATE(VendGrp)
BEGIN
    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
                 ON d.APCo = i.APCo AND d.Mth = i.Mth AND d.APTrans = i.APTrans
                WHERE d.PostedYN = 'Y'
                  AND (i.Vendor <> d.Vendor OR i.VendGrp <> d.VendGrp
                       OR i.APRef <> d.APRef OR i.GrossAmt <> d.GrossAmt))
    BEGIN
        SELECT @errmsg = 'Vendor, invoice # and amount cannot be changed on a posted invoice.'
        GOTO error
    END
END

IF NOT UPDATE(Status) RETURN

-- ---------------------------------------------------------------------------
-- allowed transitions
--   O -> P, A, V        P -> A, O, V        A -> H, X, V
--   H -> A, V           X, V  final
-- ---------------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
             ON d.APCo = i.APCo AND d.Mth = i.Mth AND d.APTrans = i.APTrans
            WHERE i.Status <> d.Status
              AND NOT (   (d.Status = 'O' AND i.Status IN ('P', 'A', 'V'))
                       OR (d.Status = 'P' AND i.Status IN ('A', 'O', 'V'))
                       OR (d.Status = 'A' AND i.Status IN ('H', 'X', 'V'))
                       OR (d.Status = 'H' AND i.Status IN ('A', 'V'))))
BEGIN
    SELECT @errmsg = 'Invalid invoice status change.'
    GOTO error
END

-- paid only with no blocking holds. RET hold only withholds the retainage
-- portion so it does not stop the payment of the rest.
IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
             ON d.APCo = i.APCo AND d.Mth = i.Mth AND d.APTrans = i.APTrans
            WHERE i.Status = 'X' AND d.Status <> 'X'
              AND EXISTS (SELECT 1 FROM dbo.APHold h
                           WHERE h.APCo = i.APCo AND h.Mth = i.Mth AND h.APTrans = i.APTrans
                             AND h.RelDate IS NULL AND h.HoldCode <> 'RET'))
BEGIN
    SELECT @errmsg = 'Invoice has open holds and cannot be paid.'
    GOTO error
END

-- void reason
IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d
             ON d.APCo = i.APCo AND d.Mth = i.Mth AND d.APTrans = i.APTrans
            WHERE i.Status = 'V' AND d.Status <> 'V'
              AND LEN(LTRIM(RTRIM(ISNULL(i.VoidReason, '')))) = 0)
BEGIN
    SELECT @errmsg = 'A void reason is required.'
    GOTO error
END

-- ---------------------------------------------------------------------------
-- audit
-- ---------------------------------------------------------------------------
INSERT dbo.APInvAudit (APCo, Mth, APTrans, OldStatus, NewStatus, ChgBy, ChgDate)
SELECT i.APCo, i.Mth, i.APTrans, d.Status, i.Status, SUSER_SNAME(), GETDATE()
  FROM inserted i JOIN deleted d
    ON d.APCo = i.APCo AND d.Mth = i.Mth AND d.APTrans = i.APTrans
 WHERE i.Status <> d.Status

RETURN

error:
    RAISERROR(@errmsg, 11, -1)
    ROLLBACK TRANSACTION
GO
