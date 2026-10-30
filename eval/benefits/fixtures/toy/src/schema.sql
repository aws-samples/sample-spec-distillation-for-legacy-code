-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic toy fixture for eval/benefits self-tests. Not customer code.
CREATE TABLE Invoice (
    InvoiceId   INT IDENTITY PRIMARY KEY,
    VendorCode  VARCHAR(10)   NOT NULL,
    InvoiceNo   VARCHAR(20)   NOT NULL,
    Total       DECIMAL(12,2) NOT NULL CHECK (Total > 0),
    Status      VARCHAR(20)   NOT NULL,
    CreatedAt   DATETIME      NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_Invoice_Vendor_No UNIQUE (VendorCode, InvoiceNo)
);
GO

CREATE PROCEDURE usp_SaveInvoice
    @Vendor VARCHAR(10), @InvoiceNo VARCHAR(20),
    @Total DECIMAL(12,2), @Status VARCHAR(20)
AS
BEGIN
    IF @Total > 50000 AND @Status = 'APPROVED'
        SET @Status = 'PENDING_APPROVAL';
    INSERT INTO Invoice (VendorCode, InvoiceNo, Total, Status)
    VALUES (@Vendor, @InvoiceNo, @Total, @Status);
END
GO
