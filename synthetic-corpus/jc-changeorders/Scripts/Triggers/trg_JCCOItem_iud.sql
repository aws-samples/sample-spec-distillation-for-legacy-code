-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- trg_JCCOItem_iud   insert / update / delete trigger on JCCOItem
--   items can only change while the CO is Draft (or Rejected, being reworked)
--   DWH 2006-08
-- =============================================================================
IF OBJECT_ID('dbo.trg_JCCOItem_iud') IS NOT NULL DROP TRIGGER dbo.trg_JCCOItem_iud
GO
CREATE TRIGGER dbo.trg_JCCOItem_iud ON dbo.JCCOItem
FOR INSERT, UPDATE, DELETE
AS
SET NOCOUNT ON

DECLARE @errmsg varchar(255)

IF EXISTS (SELECT 1
             FROM (SELECT JCCo, Job, CONum FROM inserted
                   UNION
                   SELECT JCCo, Job, CONum FROM deleted) x
             JOIN dbo.JCCOHdr h ON h.JCCo = x.JCCo AND h.Job = x.Job AND h.CONum = x.CONum
            WHERE h.Status NOT IN ('D', 'R'))
BEGIN
    -- usp_JCCOSubmit re-rounds item amounts while the CO is still Draft,
    -- so that path never hits this
    SELECT @errmsg = 'Items can only be changed on a draft change order.'
    RAISERROR(@errmsg, 11, -1)
    ROLLBACK TRANSACTION
    RETURN
END

IF EXISTS (SELECT 1 FROM inserted WHERE Units < 0 AND UnitCost < 0)
BEGIN
    SELECT @errmsg = 'Units and unit cost cannot both be negative.'
    RAISERROR(@errmsg, 11, -1)
    ROLLBACK TRANSACTION
    RETURN
END
GO
