-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic fixture for identifier_evidence tests (no customer code).
CREATE TABLE dbo.ShipHdr (
    ShipNo    int           NOT NULL,
    XmitFlg   char(1)       NOT NULL DEFAULT ('N'),
    XmitDate  datetime      NULL,
    HzdCls    varchar(4)    NULL,
    PkgWgt    numeric(9,2)  NOT NULL DEFAULT (0)  -- package weight in kg
)
GO
