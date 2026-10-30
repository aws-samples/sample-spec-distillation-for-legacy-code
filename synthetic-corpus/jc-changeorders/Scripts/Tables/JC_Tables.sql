-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
-- =============================================================================
-- JC_Tables.sql   JC change order / subcontract tables + lookups
-- rev 2006-08 DWH, 2010-02 MJR (JCCOApprLim), 2014-09 TKB (field sync cols),
--     2018-05 TKB (JCAdmUsers)
-- =============================================================================
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO

-- -----------------------------------------------------------------------------
-- company parameters
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCoParm (
    JCCo              tinyint       NOT NULL,
    GLCo              tinyint       NOT NULL,
    COApprThreshold   numeric(12,2) NOT NULL CONSTRAINT dfJCCoParm_ApprThr DEFAULT (50000.00),
    DfltMarkupPct     numeric(6,3)  NOT NULL CONSTRAINT dfJCCoParm_Mkup DEFAULT (10.000),
    MaxMarkupPct      numeric(6,3)  NOT NULL CONSTRAINT dfJCCoParm_MaxMkup DEFAULT (15.000),
    DfltMaxCOPct      numeric(6,3)  NOT NULL CONSTRAINT dfJCCoParm_MaxCO DEFAULT (25.000),
    FieldMaxAmt       numeric(12,2) NOT NULL CONSTRAINT dfJCCoParm_FldMax DEFAULT (25000.00),
    NoticeDays        smallint      NOT NULL CONSTRAINT dfJCCoParm_Notice DEFAULT (21),
    FeeOnFeeYN        char(1)       NOT NULL CONSTRAINT dfJCCoParm_FoF DEFAULT ('N'),
    CONSTRAINT pkJCCoParm PRIMARY KEY CLUSTERED (JCCo)
)
GO

-- -----------------------------------------------------------------------------
-- jobs / contracts
--   JobStatus 1 open, 2 soft closed, 3 hard closed
--   ContractType LS lump sum, TM time and material, CP cost plus, GMP guaranteed max
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCJob (
    JCCo          tinyint       NOT NULL,
    Job           varchar(10)   NOT NULL,
    Description   varchar(60)   NULL,
    JobStatus     tinyint       NOT NULL CONSTRAINT dfJCJob_Stat DEFAULT (1),
    ContractType  varchar(3)    NOT NULL CONSTRAINT dfJCJob_CT DEFAULT ('LS'),
    OrigContract  numeric(14,2) NOT NULL CONSTRAINT dfJCJob_Orig DEFAULT (0),
    RevContract   numeric(14,2) NOT NULL CONSTRAINT dfJCJob_Rev DEFAULT (0),
    TaxRate       numeric(8,6)  NOT NULL CONSTRAINT dfJCJob_Tax DEFAULT (0),
    ProjMgr       varchar(30)   NULL,
    ProjExec      varchar(30)   NULL,
    CONSTRAINT pkJCJob PRIMARY KEY CLUSTERED (JCCo, Job),
    CONSTRAINT ckJCJob_Stat CHECK (JobStatus IN (1, 2, 3)),
    CONSTRAINT ckJCJob_CT CHECK (ContractType IN ('LS', 'TM', 'CP', 'GMP'))
)
GO

-- -----------------------------------------------------------------------------
-- subcontracts
--   SLStatus 0 open, 1 complete, 2 closed
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.SLHdr (
    SLCo        tinyint       NOT NULL,
    SL          varchar(12)   NOT NULL,
    JCCo        tinyint       NOT NULL,
    Job         varchar(10)   NOT NULL,
    VendGrp     tinyint       NOT NULL,
    Vendor      int           NOT NULL,
    Description varchar(60)   NULL,
    SLStatus    tinyint       NOT NULL CONSTRAINT dfSLHdr_Stat DEFAULT (0),
    OrigCost    numeric(14,2) NOT NULL CONSTRAINT dfSLHdr_Orig DEFAULT (0),
    CurCost     numeric(14,2) NOT NULL CONSTRAINT dfSLHdr_Cur DEFAULT (0),
    RetgPct     numeric(6,3)  NOT NULL CONSTRAINT dfSLHdr_Retg DEFAULT (10.000),
    MaxCOPct    numeric(6,3)  NULL,          -- null = JCCoParm.DfltMaxCOPct
    InsExpDate  smalldatetime NULL,
    CONSTRAINT pkSLHdr PRIMARY KEY CLUSTERED (SLCo, SL),
    CONSTRAINT ckSLHdr_Retg CHECK (RetgPct BETWEEN 0 AND 10)
)
GO

-- -----------------------------------------------------------------------------
-- change order header
--   COType   O owner, S subcontract, I internal (budget transfer)
--   Status   D draft, P pending approval, A approved, R rejected, V void,
--            I interfaced (posted to contract / subcontract)
--   SourceSys ERP | FLD (field app import)
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCOHdr (
    JCCo         tinyint       NOT NULL,
    Job          varchar(10)   NOT NULL,
    CONum        varchar(3)    NOT NULL,
    Description  varchar(60)   NOT NULL,
    COType       char(1)       NOT NULL,
    ReasonCode   varchar(3)    NOT NULL,
    Status       char(1)       NOT NULL CONSTRAINT dfJCCOHdr_Stat DEFAULT ('D'),
    SL           varchar(12)   NULL,
    ReqDate      smalldatetime NOT NULL,
    ReqBy        varchar(30)   NULL,
    EntBy        varchar(30)   NOT NULL,
    EntDate      datetime      NOT NULL CONSTRAINT dfJCCOHdr_Ent DEFAULT (GETDATE()),
    SubmitDate   datetime      NULL,
    ApprBy       varchar(30)   NULL,
    ApprDate     datetime      NULL,
    RejReason    varchar(255)  NULL,
    VoidReason   varchar(255)  NULL,
    CostAmt      numeric(12,2) NOT NULL CONSTRAINT dfJCCOHdr_Cost DEFAULT (0),
    MarkupPct    numeric(6,3)  NOT NULL CONSTRAINT dfJCCOHdr_MkPct DEFAULT (0),
    MarkupAmt    numeric(12,2) NOT NULL CONSTRAINT dfJCCOHdr_MkAmt DEFAULT (0),
    TaxAmt       numeric(12,2) NOT NULL CONSTRAINT dfJCCOHdr_Tax DEFAULT (0),
    TotalAmt     numeric(12,2) NOT NULL CONSTRAINT dfJCCOHdr_Tot DEFAULT (0),
    DaysExt      smallint      NOT NULL CONSTRAINT dfJCCOHdr_Days DEFAULT (0),
    OwnerSignDate smalldatetime NULL,
    OvrFlag      char(1)       NOT NULL CONSTRAINT dfJCCOHdr_Ovr DEFAULT ('N'),
    SourceSys    varchar(3)    NOT NULL CONSTRAINT dfJCCOHdr_Src DEFAULT ('ERP'),
    ExtRef       varchar(36)   NULL,
    ExpFlag      char(1)       NOT NULL CONSTRAINT dfJCCOHdr_Exp DEFAULT ('N'),
    ExpDate      datetime      NULL,
    CONSTRAINT pkJCCOHdr PRIMARY KEY CLUSTERED (JCCo, Job, CONum),
    CONSTRAINT ckJCCOHdr_Type CHECK (COType IN ('O', 'S', 'I')),
    CONSTRAINT ckJCCOHdr_Stat CHECK (Status IN ('D', 'P', 'A', 'R', 'V', 'I')),
    CONSTRAINT ckJCCOHdr_Mkup CHECK (MarkupPct BETWEEN 0 AND 15),
    CONSTRAINT ckJCCOHdr_Days CHECK (DaysExt BETWEEN 0 AND 365)
)
GO

CREATE UNIQUE NONCLUSTERED INDEX uxJCCOHdr_ExtRef ON dbo.JCCOHdr (ExtRef)
    WHERE ExtRef IS NOT NULL
GO

-- -----------------------------------------------------------------------------
-- change order items
--   CostType L labor, M material, E equipment, S subcontract, O other
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCOItem (
    JCCo      tinyint       NOT NULL,
    Job       varchar(10)   NOT NULL,
    CONum     varchar(3)    NOT NULL,
    Item      smallint      NOT NULL,
    Phase     varchar(20)   NOT NULL,
    CostType  char(1)       NOT NULL,
    Units     numeric(12,3) NOT NULL CONSTRAINT dfJCCOItem_U DEFAULT (0),
    UM        varchar(3)    NOT NULL CONSTRAINT dfJCCOItem_UM DEFAULT ('LS'),
    UnitCost  numeric(12,4) NOT NULL CONSTRAINT dfJCCOItem_UC DEFAULT (0),
    Amount    numeric(12,2) NOT NULL CONSTRAINT dfJCCOItem_Amt DEFAULT (0),
    TaxYN     char(1)       NOT NULL CONSTRAINT dfJCCOItem_Tax DEFAULT ('N'),
    CONSTRAINT pkJCCOItem PRIMARY KEY CLUSTERED (JCCo, Job, CONum, Item),
    CONSTRAINT ckJCCOItem_CT CHECK (CostType IN ('L', 'M', 'E', 'S', 'O'))
)
GO

-- -----------------------------------------------------------------------------
-- reason codes
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCOReason (
    ReasonCode  varchar(3)  NOT NULL,
    Description varchar(40) NOT NULL,
    ActiveYN    char(1)     NOT NULL CONSTRAINT dfJCCOReason_Act DEFAULT ('Y'),
    CONSTRAINT pkJCCOReason PRIMARY KEY CLUSTERED (ReasonCode)
)
GO

-- -----------------------------------------------------------------------------
-- approval limits by role
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCOApprLim (
    JCCo     tinyint       NOT NULL,
    Role     varchar(5)    NOT NULL,
    MaxAmt   numeric(14,2) NOT NULL,
    CONSTRAINT pkJCCOApprLim PRIMARY KEY CLUSTERED (JCCo, Role)
)
GO

CREATE TABLE dbo.SecUser (
    UserName  varchar(30) NOT NULL,
    JCRole    varchar(5)  NULL,     -- ENTRY, PM, PX, VP, CFO
    ActiveYN  char(1)     NOT NULL CONSTRAINT dfSecUser_Act DEFAULT ('Y'),
    CONSTRAINT pkSecUser PRIMARY KEY CLUSTERED (UserName)
)
GO

CREATE TABLE dbo.JCAdmUsers (
    UserName  varchar(30) NOT NULL,
    AddedBy   varchar(30) NULL,
    AddedDate datetime    NULL,
    CONSTRAINT pkJCAdmUsers PRIMARY KEY CLUSTERED (UserName)
)
GO

-- -----------------------------------------------------------------------------
-- committed cost (read by AP when the sub invoices against the SL)
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCommit (
    JCCo      tinyint       NOT NULL,
    Job       varchar(10)   NOT NULL,
    Phase     varchar(20)   NOT NULL,
    CostType  char(1)       NOT NULL,
    SL        varchar(12)   NOT NULL,
    CONum     varchar(3)    NOT NULL,
    CmtdAmt   numeric(12,2) NOT NULL,
    PostDate  datetime      NOT NULL,
    CONSTRAINT pkJCCommit PRIMARY KEY CLUSTERED (JCCo, Job, SL, CONum, Phase, CostType)
)
GO

-- -----------------------------------------------------------------------------
-- audit
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.JCCOAudit (
    AuditId    int IDENTITY(1,1) NOT NULL,
    JCCo       tinyint       NOT NULL,
    Job        varchar(10)   NOT NULL,
    CONum      varchar(3)    NOT NULL,
    OldStatus  char(1)       NULL,
    NewStatus  char(1)       NULL,
    ChgBy      varchar(128)  NOT NULL,
    ChgDate    datetime      NOT NULL CONSTRAINT dfJCCOAudit_Dt DEFAULT (GETDATE()),
    Note       varchar(255)  NULL,
    CONSTRAINT pkJCCOAudit PRIMARY KEY CLUSTERED (AuditId)
)
GO

-- =============================================================================
-- seed rows
-- =============================================================================
INSERT dbo.JCCoParm (JCCo, GLCo, COApprThreshold, DfltMarkupPct, MaxMarkupPct, DfltMaxCOPct,
                     FieldMaxAmt, NoticeDays, FeeOnFeeYN)
VALUES (1, 1, 50000.00, 10.000, 15.000, 25.000, 25000.00, 21, 'N')
GO

INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('OWN', 'Owner requested change', 'Y')
INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('UNF', 'Unforeseen site condition', 'Y')
INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('DES', 'Design error / omission', 'Y')
INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('VAL', 'Value engineering credit', 'Y')
INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('SCH', 'Schedule / acceleration', 'Y')
INSERT dbo.JCCOReason (ReasonCode, Description, ActiveYN) VALUES ('ALW', 'Allowance reconciliation', 'N')
GO

INSERT dbo.JCCOApprLim (JCCo, Role, MaxAmt) VALUES (1, 'ENTRY', 0.00)
INSERT dbo.JCCOApprLim (JCCo, Role, MaxAmt) VALUES (1, 'PM', 50000.00)
INSERT dbo.JCCOApprLim (JCCo, Role, MaxAmt) VALUES (1, 'PX', 250000.00)
INSERT dbo.JCCOApprLim (JCCo, Role, MaxAmt) VALUES (1, 'VP', 1000000.00)
INSERT dbo.JCCOApprLim (JCCo, Role, MaxAmt) VALUES (1, 'CFO', 999999999.99)
GO

INSERT dbo.JCJob (JCCo, Job, Description, JobStatus, ContractType, OrigContract, RevContract, TaxRate, ProjMgr, ProjExec)
VALUES (1, '24-101', 'Riverside Clinic Expansion', 1, 'LS', 4250000.00, 4250000.00, 0.0825, 'jkessler', 'mtorres')
INSERT dbo.JCJob (JCCo, Job, Description, JobStatus, ContractType, OrigContract, RevContract, TaxRate, ProjMgr, ProjExec)
VALUES (1, '24-102', 'Elm St Parking Deck', 1, 'TM', 0.00, 0.00, 0.0825, 'apatel', 'mtorres')
INSERT dbo.JCJob (JCCo, Job, Description, JobStatus, ContractType, OrigContract, RevContract, TaxRate, ProjMgr, ProjExec)
VALUES (1, '23-077', 'Northgate School Reroof', 2, 'GMP', 1180000.00, 1212500.00, 0.0700, 'jkessler', 'rlinden')
GO

INSERT dbo.SLHdr (SLCo, SL, JCCo, Job, VendGrp, Vendor, Description, SLStatus, OrigCost, CurCost, RetgPct, MaxCOPct, InsExpDate)
VALUES (1, '24-101-003', 1, '24-101', 1, 2210, 'Electrical', 0, 612000.00, 640500.00, 10.000, NULL, '2027-03-31')
INSERT dbo.SLHdr (SLCo, SL, JCCo, Job, VendGrp, Vendor, Description, SLStatus, OrigCost, CurCost, RetgPct, MaxCOPct, InsExpDate)
VALUES (1, '24-101-007', 1, '24-101', 1, 3305, 'Drywall and framing', 0, 288000.00, 288000.00, 5.000, 15.000, '2026-09-30')
GO

INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('lchen', 'ENTRY', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('jkessler', 'PM', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('apatel', 'PM', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('mtorres', 'PX', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('rlinden', 'VP', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('dfarrow', 'CFO', 'Y')
INSERT dbo.SecUser (UserName, JCRole, ActiveYN) VALUES ('FIELDAPI', 'ENTRY', 'Y')
GO

INSERT dbo.JCAdmUsers (UserName, AddedBy, AddedDate) VALUES ('tbrandt', 'TKB', '2018-05-14')
GO
