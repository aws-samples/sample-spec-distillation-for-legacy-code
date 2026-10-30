' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' modAPGlobals.vb
' AP module shared constants / globals
' Orig: RDM 03/2004   Rev: KLT 11/2009, PSV 06/2013, RDM 02/2016
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data.SqlClient

Public Module modAPGlobals

    '--- invoice status codes (APInvHdr.Status) -------------------------------
    Public Const STAT_OPEN As String = "O"
    Public Const STAT_PENDAPPR As String = "P"
    Public Const STAT_APPROVED As String = "A"
    Public Const STAT_HOLD As String = "H"
    Public Const STAT_PAID As String = "X"      ' X = paid/cleared (left over from check rec days)
    Public Const STAT_VOID As String = "V"

    '--- AP roles (SecUser.APRole) ---------------------------------------------
    Public Const ROLE_CLERK As String = "APCLK"
    Public Const ROLE_PM As String = "PM"
    Public Const ROLE_CTRL As String = "CTRL"
    Public Const ROLE_CFO As String = "CFO"
    Public Const ROLE_RISK As String = "RISK"

    '--- hold codes (APHoldCode) -----------------------------------------------
    Public Const HOLD_LIENWVR As String = "LW"
    Public Const HOLD_INSUR As String = "INS"
    Public Const HOLD_DISPUTE As String = "DSP"
    Public Const HOLD_MGR As String = "MGR"
    Public Const HOLD_RETG As String = "RET"

    '--- line types (APInvLine.LineType) ---------------------------------------
    Public Const LT_JOB As Integer = 1
    Public Const LT_EXP As Integer = 2
    Public Const LT_EQUIP As Integer = 3     ' not used since equipment module retired

    '--- thresholds ------------------------------------------------------------
    ' invoices at or above this need approval. (APCoParm.ApprThreshold has the same
    ' number - kept here so the screen doesn't have to round trip. KLT)
    Public Const APPR_THRESHOLD As Decimal = 10000D

    Public Const DFLT_PAY_TERMS As Integer = 30
    Public Const MAX_APREF_LEN As Integer = 15

    ' approval limits by role. Should match APApprLim table.
    Public Function GetRoleApprLimit(ByVal sRole As String) As Decimal
        Select Case UCase(Trim(sRole))
            Case ROLE_CLERK
                Return 0D
            Case ROLE_PM
                Return 25000D
            Case ROLE_CTRL
                Return 100000D
            Case ROLE_CFO
                Return 999999999.99D
            Case Else
                Return 0D
        End Select
    End Function

    '--- session ---------------------------------------------------------------
    Public g_APCo As Byte = 1
    Public g_UserId As String = ""
    Public g_UserRole As String = ""
    Public g_ConnStr As String = ""

    Public Function GetConn() As SqlConnection
        Dim cn As New SqlConnection(g_ConnStr)
        cn.Open()
        Return cn
    End Function

    Public Function StatusText(ByVal s As String) As String
        Select Case s
            Case STAT_OPEN : Return "Open"
            Case STAT_PENDAPPR : Return "Pending Approval"
            Case STAT_APPROVED : Return "Approved"
            Case STAT_HOLD : Return "On Hold"
            Case STAT_PAID : Return "Paid"
            Case STAT_VOID : Return "Voided"
            Case Else : Return "?" & s
        End Select
    End Function

    ' first-of-month normalize. Mth / UIMth are always 1st of month in the db.
    Public Function FirstOfMonth(ByVal d As Date) As Date
        Return New Date(d.Year, d.Month, 1)
    End Function

End Module
