' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' clsJCCOBL.vb
' Client side business rules for change orders. The procs re-check most of
' this (usp_JCCOSubmit / usp_JCCOApprove) - these are here so the user gets a
' message before the round trip.
'   DWH 08/2006 created
'   MJR 02/2010 approval limits, sub CO cap
'   TKB 09/2014 field app
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Collections.Generic

Public Class clsJCCOBL

    Private m_DAL As clsJCCODAL
    Private m_Errors As New List(Of String)

    Public Sub New()
        m_DAL = New clsJCCODAL
    End Sub

    Public Sub New(ByVal dal As clsJCCODAL)
        m_DAL = dal
    End Sub

    Public ReadOnly Property Errors() As List(Of String)
        Get
            Return m_Errors
        End Get
    End Property

    Public ReadOnly Property ErrorText() As String
        Get
            Return String.Join(vbCrLf, m_Errors.ToArray())
        End Get
    End Property

    '--------------------------------------------------------------------------
    ' amounts
    '--------------------------------------------------------------------------
    Public Function CalcItemAmount(ByVal units As Decimal, ByVal unitCost As Decimal) As Decimal
        Return Math.Round(units * unitCost, 2)
    End Function

    ' Cost = sum of item amounts. Markup on cost, tax on taxable cost only.
    ' Total = cost + markup + tax
    Public Sub CalcTotals(ByVal dtItems As DataTable, ByVal markupPct As Decimal, ByVal taxRate As Decimal, _
                          ByRef costAmt As Decimal, ByRef markupAmt As Decimal, _
                          ByRef taxAmt As Decimal, ByRef totalAmt As Decimal)
        Dim taxable As Decimal = 0D
        costAmt = 0D
        For Each r As DataRow In dtItems.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            Dim amt As Decimal = CDec(r("Amount"))
            costAmt += amt
            If CStr(r("TaxYN")) = "Y" Then taxable += amt
        Next
        markupAmt = Math.Round(costAmt * markupPct / 100D, 2)
        taxAmt = Math.Round(taxable * taxRate, 2)
        totalAmt = costAmt + markupAmt + taxAmt
    End Sub

    '--------------------------------------------------------------------------
    ' header validation (entry screen, before save / submit)
    '--------------------------------------------------------------------------
    Public Function ValidateHeader(ByVal hdr As DataRow, ByVal job As DataRow, ByVal totalAmt As Decimal) As Boolean
        m_Errors.Clear()

        If job Is Nothing Then
            m_Errors.Add("Job not found.")
            Return False
        End If

        Dim js As Integer = CInt(job("JobStatus"))
        If js = JOB_CLOSED OrElse js = JOB_SOFTCLOSED Then
            m_Errors.Add("Job " & CStr(job("Job")) & " is closed. Change orders cannot be added.")
        End If

        Dim sDesc As String = IIf(IsDBNull(hdr("Description")), "", CStr(hdr("Description"))).ToString().Trim()
        If sDesc.Length = 0 Then
            m_Errors.Add("Description is required.")
        ElseIf sDesc.Length > MAX_DESC_LEN Then
            m_Errors.Add("Description cannot be longer than " & MAX_DESC_LEN & " characters.")
        End If

        Dim sType As String = CStr(hdr("COType"))
        If sType <> COTYPE_OWNER AndAlso sType <> COTYPE_SUB AndAlso sType <> COTYPE_INTERNAL Then
            m_Errors.Add("Invalid change order type.")
        End If
        If sType = COTYPE_SUB AndAlso (IsDBNull(hdr("SL")) OrElse CStr(hdr("SL")).Trim() = "") Then
            m_Errors.Add("A subcontract must be selected for a subcontract change order.")
        End If

        If Not m_DAL.IsReasonActive(CStr(hdr("ReasonCode"))) Then
            m_Errors.Add("Reason code is not valid.")
        End If

        ' value engineering is always a credit back to the owner
        If CStr(hdr("ReasonCode")) = "VAL" AndAlso totalAmt >= 0D Then
            m_Errors.Add("Value engineering change orders must be a deduct (negative amount).")
        End If

        Dim pct As Decimal = CDec(hdr("MarkupPct"))
        If pct < 0D OrElse pct > MAX_MARKUP_PCT Then
            m_Errors.Add("Markup must be between 0 and " & MAX_MARKUP_PCT & "%.")
        End If

        If CDate(hdr("ReqDate")) > Today Then
            m_Errors.Add("Request date cannot be in the future.")
        End If

        Dim days As Integer = CInt(hdr("DaysExt"))
        If days < 0 OrElse days > MAX_DAYS_EXT Then
            m_Errors.Add("Schedule extension must be 0 to " & MAX_DAYS_EXT & " days.")
        End If

        Return m_Errors.Count = 0
    End Function

    Public Function ValidateItems(ByVal dtItems As DataTable) As Boolean
        Dim n As Integer = 0
        For Each r As DataRow In dtItems.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            n += 1
            If IsDBNull(r("Phase")) OrElse CStr(r("Phase")).Trim() = "" Then
                m_Errors.Add("Item " & n & ": phase is required.")
            End If
            Dim ct As String = CStr(r("CostType"))
            If "LMESO".IndexOf(ct) < 0 OrElse ct.Length <> 1 Then
                m_Errors.Add("Item " & n & ": invalid cost type.")
            End If
        Next
        If n = 0 Then m_Errors.Add("At least one item is required.")
        Return m_Errors.Count = 0
    End Function

    '--------------------------------------------------------------------------
    ' approval
    '--------------------------------------------------------------------------
    Public Function RequiresExecApproval(ByVal totalAmt As Decimal) As Boolean
        Return totalAmt >= CO_PM_LIMIT
    End Function

    Public Function CanApprove(ByVal hdr As DataRow, ByVal sUser As String, ByVal sRole As String) As Boolean
        m_Errors.Clear()
        If CStr(hdr("Status")) <> CO_PENDING Then
            m_Errors.Add("Only pending change orders can be approved.")
            Return False
        End If
        If String.Compare(CStr(hdr("EntBy")), sUser, True) = 0 Then
            m_Errors.Add("You cannot approve a change order you entered.")
        End If
        Dim tot As Decimal = CDec(hdr("TotalAmt"))
        If sRole = ROLE_PM AndAlso RequiresExecApproval(tot) Then
            m_Errors.Add("Change orders of " & Format(CO_PM_LIMIT, "$#,##0") & " or more need executive approval.")
        ElseIf GetRoleApprLimit(sRole) < tot Then
            m_Errors.Add("Amount is over your approval limit.")
        End If
        ' Owner change orders need the owner's countersigned copy on file
        ' (OwnerSignDate) before they can go to Approved. Contract admin
        ' policy 2012 - approver checks the paper file.
        Return m_Errors.Count = 0
    End Function

    ' one step save + approve for small COs, PM only
    Public Function CanQuickApprove(ByVal sRole As String, ByVal previewTotal As Decimal) As Boolean
        If sRole = ROLE_ENTRY OrElse sRole = "" Then Return False
        Return previewTotal <= MINOR_CO_LIMIT
    End Function

    '--------------------------------------------------------------------------
    ' subcontract CO cap
    ' total change orders on a subcontract cannot go over MaxCOPct of the
    ' original subcontract value (default JCCoParm.DfltMaxCOPct)
    '--------------------------------------------------------------------------
    Public Function CheckSubCap(ByVal sl As DataRow, ByVal coCost As Decimal, ByVal dfltPct As Decimal) As Boolean
        Dim orig As Decimal = CDec(sl("OrigCost"))
        Dim cur As Decimal = CDec(sl("CurCost"))
        Dim pct As Decimal = IIf(IsDBNull(sl("MaxCOPct")), dfltPct, sl("MaxCOPct"))
        Dim capAmt As Decimal = Math.Round(orig * pct / 100D, 2)
        If (cur - orig) + coCost > capAmt Then
            m_Errors.Add("This change order would put subcontract " & CStr(sl("SL")) & _
                         " over its change order limit of " & Format(capAmt, "$#,##0.00") & ".")
            Return False
        End If
        Return True
    End Function

    Public Function SubCapRemaining(ByVal sl As DataRow, ByVal dfltPct As Decimal) As Decimal
        Dim orig As Decimal = CDec(sl("OrigCost"))
        Dim cur As Decimal = CDec(sl("CurCost"))
        Dim pct As Decimal = IIf(IsDBNull(sl("MaxCOPct")), dfltPct, sl("MaxCOPct"))
        Return Math.Round(orig * pct / 100D, 2) - (cur - orig)
    End Function

    '--------------------------------------------------------------------------
    ' status transitions (keep in sync with trg_JCCOHdr_u)
    '--------------------------------------------------------------------------
    Public Function IsTransitionAllowed(ByVal sFrom As String, ByVal sTo As String) As Boolean
        Select Case sFrom & ">" & sTo
            Case "D>P", "D>V", "P>A", "P>R", "R>D", "R>V", "A>I", "A>V"
                Return True
            Case Else
                Return False
        End Select
    End Function

    Public Function CanVoid(ByVal sStatus As String, ByVal sReason As String) As Boolean
        m_Errors.Clear()
        If sStatus = CO_INTERFACED Then
            m_Errors.Add("Interfaced change orders cannot be voided. Enter a reversing change order.")
        ElseIf sStatus = CO_VOID Then
            m_Errors.Add("Change order is already void.")
        End If
        If sReason Is Nothing OrElse sReason.Trim() = "" Then
            m_Errors.Add("Void reason is required.")
        End If
        Return m_Errors.Count = 0
    End Function

    '--------------------------------------------------------------------------
    ' fee on fee - markup applied on top of the sub's own markup when
    ' JCCoParm.FeeOnFeeYN = Y.  (DWH 2006, owner contracts stopped allowing
    ' this in 2011)
    '--------------------------------------------------------------------------
    Public Function ApplyFeeOnFee(ByVal subTotal As Decimal, ByVal subMarkupPct As Decimal, _
                                  ByVal ourPct As Decimal) As Decimal
        Dim subMk As Decimal = Math.Round(subTotal * subMarkupPct / 100D, 2)
        Dim ours As Decimal = Math.Round((subTotal + subMk) * ourPct / 100D, 2)
        Return subTotal + subMk + ours
    End Function

    '--------------------------------------------------------------------------
    ' field app status names
    '--------------------------------------------------------------------------
    Public Function FieldStatus(ByVal s As String) As String
        Select Case s
            Case CO_DRAFT : Return "RECEIVED"
            Case CO_PENDING : Return "PENDING"
            Case CO_APPROVED, CO_INTERFACED : Return "APPROVED"
            Case CO_REJECTED : Return "REJECTED"
            Case CO_VOID : Return "VOID"
            Case Else : Return "UNKNOWN"
        End Select
    End Function

End Class
