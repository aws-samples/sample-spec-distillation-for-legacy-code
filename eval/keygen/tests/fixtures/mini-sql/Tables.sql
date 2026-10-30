-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic fixture for eval/keygen tests. Not real code.
CREATE TABLE dbo.Invoice (
    InvoiceId   int           NOT NULL PRIMARY KEY,
    InvoiceNo   varchar(20)   NOT NULL UNIQUE,
    VendorId    int           NOT NULL REFERENCES dbo.Vendor(VendorId),
    Amount      numeric(12,2) NOT NULL,
    Status      char(1)       NOT NULL DEFAULT ('O'),
    CONSTRAINT ckInvoiceAmount CHECK (Amount >= 0)
);
GO

CREATE TRIGGER trg_Invoice_u ON dbo.Invoice AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
END
GO

CREATE PROCEDURE dbo.usp_InvoiceApprove @InvoiceId int AS
BEGIN
    IF EXISTS (SELECT 1 FROM dbo.Invoice WHERE InvoiceId = @InvoiceId AND Status = 'P')
        RAISERROR('Paid invoices cannot be approved', 16, 1);
END
GO
