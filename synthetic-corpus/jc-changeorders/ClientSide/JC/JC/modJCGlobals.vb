' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' modJCGlobals.vb
' JC change order module shared constants / globals
' Orig: DWH 08/2006   Rev: MJR 02/2010, TKB 09/2014, TKB 05/2018
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data.SqlClient

Public Module modJCGlobals

    '--- change order status codes (JCCOHdr.Status) ----------------------------
    Public Const CO_DRAFT As String = "D"
    Public Const CO_PENDING As String = "P"
    Public Const CO_APPROVED As String = "A"
    Public Const CO_REJECTED As String = "R"
    Public Const CO_VOID As String = "V"
    Public Const CO_INTERFACED As String = "I"

    '--- change order types (JCCOHdr.COType) -----------------------------------
    Public Const COTYPE_OWNER As String = "O"
    Public Const COTYPE_SUB As String = "S"
    Public Const COTYPE_INTERNAL As String = "I"

    '--- job status (JCJob.JobStatus) ------------------------------------------
    Public Const JOB_OPEN As Integer = 1
    Public Const JOB_SOFTCLOSED As Integer = 2
    Public Const JOB_CLOSED As Integer = 3

    '--- JC roles (SecUser.JCRole) ---------------------------------------------
    Public Const ROLE_ENTRY As String = "ENTRY"
    Public Const ROLE_PM As String = "PM"
    Public Const ROLE_PX As String = "PX"
    Public Const ROLE_VP As String = "VP"
    Public Const ROLE_CFO As String = "CFO"

    '--- limits ----------------------------------------------------------------
    ' PM can sign off COs up to this; anything bigger goes to the PX / VP.
    ' same number as JCCoParm.COApprThreshold (saves a round trip - MJR)
    Public Const CO_PM_LIMIT As Decimal = 50000D

    ' "minor" CO - PM can save and approve in one step from the entry screen
    Public Const MINOR_CO_LIMIT As Decimal = 2500D

    Public Const MAX_MARKUP_PCT As Decimal = 20D
    Public Const DFLT_MARKUP_PCT As Decimal = 10D
    Public Const MAX_DESC_LEN As Integer = 60
    Public Const MAX_DAYS_EXT As Integer = 365

    '--- field app sync --------------------------------------------------------
    Public Const FIELD_MAX_ITEMS As Integer = 20
    Public Const FIELD_SCHEMA_VER As String = "2.1"
    Public g_FieldApiUrl As String = "https://fieldsync.example.invalid/api/v2/changeorders"
    Public g_FieldApiKey As String = ""

    '--- session ---------------------------------------------------------------
    Public g_JCCo As Byte = 1
    Public g_UserName As String = ""
    Public g_UserRole As String = ""
    Public g_ConnStr As String = ""

    ' approval limits by role. Should match JCCOApprLim table.
    Public Function GetRoleApprLimit(ByVal sRole As String) As Decimal
        Select Case sRole
            Case ROLE_PM
                Return CO_PM_LIMIT
            Case ROLE_PX
                Return 250000D
            Case ROLE_VP
                Return 1000000D
            Case ROLE_CFO
                Return 999999999.99D
            Case Else
                Return 0D
        End Select
    End Function

    Public Function GetConn() As SqlConnection
        Dim cn As New SqlConnection(g_ConnStr)
        cn.Open()
        Return cn
    End Function

    Public Function StatusText(ByVal s As String) As String
        Select Case s
            Case CO_DRAFT : Return "Draft"
            Case CO_PENDING : Return "Pending Approval"
            Case CO_APPROVED : Return "Approved"
            Case CO_REJECTED : Return "Rejected"
            Case CO_VOID : Return "Void"
            Case CO_INTERFACED : Return "Interfaced"
            Case Else : Return s
        End Select
    End Function

    Public Function COTypeText(ByVal s As String) As String
        Select Case s
            Case COTYPE_OWNER : Return "Owner"
            Case COTYPE_SUB : Return "Subcontract"
            Case COTYPE_INTERNAL : Return "Internal"
            Case Else : Return s
        End Select
    End Function

    ' CO numbers are 3 char, zero padded ("001" .. "999")
    Public Function FormatCONum(ByVal n As Integer) As String
        Return n.ToString().PadLeft(3, "0"c)
    End Function

End Module
