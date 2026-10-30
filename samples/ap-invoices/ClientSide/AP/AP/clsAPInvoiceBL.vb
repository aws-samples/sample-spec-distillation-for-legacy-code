' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' clsAPInvoiceBL.vb
' AP invoice business rules (client side). Most of these are re-checked in the
' stored procs, but not all of them -- see notes.
'
' History
'   03/2004 RDM  created
'   11/2009 KLT  approval limits by role, dup check moved to proc
'   06/2013 PSV  lien waiver + insurance compliance holds
'   02/2016 RDM  retainage rounding fix (bug 2231)
'   08/2019 ???  credit memo handling
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Collections.Generic

Public Class clsAPInvoiceBL

    Private m_DAL As New clsAPInvoiceDAL
    Private m_Parms As DataRow

    Public Sub New()
        m_Parms = m_DAL.GetCoParms(g_APCo)
    End Sub

    Public ReadOnly Property DAL() As clsAPInvoiceDAL
        Get
            Return m_DAL
        End Get
    End Property

    Private Function ParmDec(ByVal col As String, ByVal dflt As Decimal) As Decimal
        If m_Parms Is Nothing OrElse IsDBNull(m_Parms(col)) Then Return dflt
        Return CDec(m_Parms(col))
    End Function

    '==========================================================================
    ' HEADER VALIDATION
    '==========================================================================
    Public Function ValidateHeader(ByVal VendGrp As Byte, ByVal Vendor As Integer, ByVal APRef As String, _
                                   ByVal InvDate As Date, ByVal Mth As Date, ByVal GrossAmt As Decimal, _
                                   ByRef errs As List(Of String)) As Boolean
        errs = New List(Of String)

        Dim v As DataRow = m_DAL.GetVendor(VendGrp, Vendor)
        If v Is Nothing Then
            errs.Add("Vendor " & Vendor & " not on file.")
        ElseIf v("ActiveYN").ToString() <> "Y" Then
            errs.Add("Vendor is inactive - cannot enter invoices.")
        End If

        Dim sRef As String = NormalizeAPRef(APRef)
        If sRef = "" Then
            errs.Add("Invoice # is required.")
        ElseIf sRef.Length > MAX_APREF_LEN Then
            errs.Add("Invoice # cannot exceed " & MAX_APREF_LEN & " characters.")
        End If

        ' no future dated invoices unless company allows it
        Dim bAllowFuture As Boolean = False
        If m_Parms IsNot Nothing AndAlso m_Parms("AllowFutureInv").ToString() = "Y" Then bAllowFuture = True
        If Not bAllowFuture AndAlso InvDate.Date > Date.Today Then
            errs.Add("Invoice date cannot be in the future.")
        End If
        If InvDate.Date < Date.Today.AddDays(-365) Then
            ' warning only, 2009 KLT - auditors wanted it flagged not blocked
            Debug.WriteLine("WARN: invoice older than one year")
        End If

        ' AP subledger closed month. GL closed month is checked in usp_APInvPost.
        If m_Parms IsNot Nothing AndAlso Not IsDBNull(m_Parms("LastMthSubClsd")) Then
            If FirstOfMonth(Mth) <= CDate(m_Parms("LastMthSubClsd")) Then
                errs.Add("Month " & Format(Mth, "MM/yy") & " is closed in AP.")
            End If
        End If

        If GrossAmt = 0D Then
            errs.Add("Invoice amount cannot be zero.")
        End If

        Return errs.Count = 0
    End Function

    Public Function NormalizeAPRef(ByVal s As String) As String
        If s Is Nothing Then Return ""
        Return UCase(Trim(s))
    End Function

    Public Function ComputeDueDate(ByVal InvDate As Date, ByVal VendPayTerms As Object) As Date
        Dim nDays As Integer = DFLT_PAY_TERMS
        If VendPayTerms IsNot Nothing AndAlso Not IsDBNull(VendPayTerms) AndAlso CInt(VendPayTerms) > 0 Then
            nDays = CInt(VendPayTerms)
        End If
        Return InvDate.Date.AddDays(nDays)
    End Function

    '==========================================================================
    ' LINE VALIDATION
    '==========================================================================
    Public Function ValidateLines(ByVal dtLines As DataTable, ByVal GrossAmt As Decimal, _
                                  ByRef errs As List(Of String)) As Boolean
        errs = New List(Of String)
        Dim tot As Decimal = 0D
        If dtLines.Rows.Count = 0 Then
            errs.Add("At least one line is required.")
            Return False
        End If
        For Each r As DataRow In dtLines.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            Dim lt As Integer = CInt(r("LineType"))
            Select Case lt
                Case LT_JOB
                    If Trim(r("Job").ToString()) = "" OrElse Trim(r("Phase").ToString()) = "" Then
                        errs.Add("Line " & r("APLine") & ": Job and Phase are required for job cost lines.")
                    Else
                        Dim js As Integer = m_DAL.GetJobStatus(g_APCo, r("Job").ToString())
                        If js = 3 Then
                            errs.Add("Line " & r("APLine") & ": Job " & r("Job") & " is closed.")
                        End If
                    End If
                Case LT_EXP
                    If Trim(r("GLAcct").ToString()) = "" Then
                        errs.Add("Line " & r("APLine") & ": GL account is required for expense lines.")
                    End If
                Case LT_EQUIP
                    errs.Add("Line " & r("APLine") & ": Equipment lines are no longer supported.")
            End Select
            tot += CDec(r("Amount")) + CDec(r("TaxAmt"))
        Next
        If RoundAmt(tot) <> RoundAmt(GrossAmt) Then
            errs.Add("Lines (" & Format(tot, "#,##0.00") & ") do not equal invoice total (" & _
                     Format(GrossAmt, "#,##0.00") & ").")
        End If
        Return errs.Count = 0
    End Function

    '==========================================================================
    ' RETAINAGE
    '==========================================================================
    ' Rounding: always half AWAY from zero to the cent. VB Math.Round defaults to
    ' banker's rounding which did not match SQL ROUND() and left pennies on the
    ' retainage payable (bug 2231). Must match usp_APRetgCompute.
    Public Function RoundAmt(ByVal d As Decimal) As Decimal
        Return Math.Round(d, 2, MidpointRounding.AwayFromZero)
    End Function

    Public Function GetDfltRetgPct(ByVal VendGrp As Byte, ByVal Vendor As Integer) As Decimal
        ' NOTE: per Controller memo (JO, 2014) utility vendors (VendType 'U') are never
        ' subject to retainage. Nothing enforces that here or in the db -- AP sets
        ' DfltRetgPct = 0 on the vendor record by hand when the vendor is set up.
        Dim v As DataRow = m_DAL.GetVendor(VendGrp, Vendor)
        If v IsNot Nothing AndAlso Not IsDBNull(v("DfltRetgPct")) Then
            Return CDec(v("DfltRetgPct"))
        End If
        Return ParmDec("DfltRetgPct", 10D)
    End Function

    ' retainage is per line, on the line amount EXCLUDING tax, job lines only.
    ' header RetgAmt = sum of line RetgAmt (never recalc from header total)
    Public Function ComputeLineRetg(ByVal LineType As Integer, ByVal Amount As Decimal, ByVal RetgPct As Decimal) As Decimal
        If LineType <> LT_JOB Then Return 0D
        If RetgPct <= 0D Then Return 0D
        Return RoundAmt(Amount * RetgPct / 100D)
    End Function

    Public Function ComputeHdrRetg(ByVal dtLines As DataTable) As Decimal
        Dim tot As Decimal = 0D
        For Each r As DataRow In dtLines.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            tot += CDec(r("RetgAmt"))
        Next
        Return tot
    End Function

    '==========================================================================
    ' APPROVAL
    '==========================================================================
    Public Function RequiresApproval(ByVal GrossAmt As Decimal) As Boolean
        ' credit memos never go to approval
        If GrossAmt < 0D Then Return False
        Return GrossAmt >= APPR_THRESHOLD
    End Function

    Public Function CanApprove(ByVal sRole As String, ByVal GrossAmt As Decimal, ByVal sEntBy As String, _
                               ByRef sWhy As String) As Boolean
        sWhy = ""
        If String.Compare(Trim(sEntBy), Trim(g_UserId), True) = 0 Then
            sWhy = "You cannot approve an invoice you entered."
            Return False
        End If
        Dim lim As Decimal = GetRoleApprLimit(sRole)
        If GrossAmt > lim Then
            sWhy = "Amount exceeds your approval limit of " & Format(lim, "$#,##0.00") & "."
            Return False
        End If
        Return True
    End Function

    '==========================================================================
    ' DUPLICATES
    '==========================================================================
    Public Function FindDuplicates(ByVal VendGrp As Byte, ByVal Vendor As Integer, ByVal APRef As String, _
                                   ByVal GrossAmt As Decimal, ByVal InvDate As Date) As DataTable
        If GrossAmt < 0D Then Return New DataTable ' credit memos skip dup check
        Return m_DAL.DupCheck(g_APCo, VendGrp, Vendor, NormalizeAPRef(APRef), GrossAmt, InvDate, Nothing, Nothing)
    End Function

    Public Function CanOverrideDup(ByVal sRole As String) As Boolean
        Return sRole = ROLE_CTRL OrElse sRole = ROLE_CFO
    End Function

    '==========================================================================
    ' HOLDS
    '==========================================================================
    Public Function CanReleaseHold(ByVal sRole As String, ByVal HoldCode As String, ByVal RelRole As String, _
                                   ByVal HoldBy As String) As Boolean
        ' insurance holds: Risk only. nobody else, not even CFO (PSV 2013)
        If HoldCode = HOLD_INSUR Then Return sRole = ROLE_RISK
        ' manager hold can be lifted by whoever put it on
        If HoldCode = HOLD_MGR AndAlso String.Compare(HoldBy, g_UserId, True) = 0 Then Return True
        ' controller / cfo can release anything else
        If sRole = ROLE_CTRL OrElse sRole = ROLE_CFO Then Return True
        Return sRole = RelRole
    End Function

    '==========================================================================
    ' PAYMENT ELIGIBILITY
    '==========================================================================
    Public Function CanPay(ByVal hdr As DataRow, ByRef reasons As List(Of String)) As Boolean
        reasons = New List(Of String)
        Dim st As String = hdr("Status").ToString()
        If st <> STAT_APPROVED Then
            reasons.Add("Invoice status is " & StatusText(st) & ".")
        End If

        Dim dtH As DataTable = m_DAL.GetOpenHolds(CByte(hdr("APCo")), CDate(hdr("Mth")), CInt(hdr("APTrans")))
        Dim nHolds As Integer = 0
        For Each h As DataRow In dtH.Rows
            ' RET only withholds the retainage portion, the rest can still be paid
            If h("HoldCode").ToString() <> HOLD_RETG Then nHolds += 1
        Next
        If nHolds > 0 Then reasons.Add(nHolds & " open hold(s).")

        ' lien waiver over threshold (job invoices only)
        Dim lwThr As Decimal = ParmDec("LienWvrThreshold", 5000D)
        If Not IsDBNull(hdr("Job")) AndAlso CDec(hdr("GrossAmt")) >= lwThr _
           AndAlso hdr("LienWvrRcvd").ToString() <> "Y" Then
            reasons.Add("Lien waiver not received.")
        End If

        ' vendor insurance
        Dim dtC As DataTable = m_DAL.GetVendCompliance(CByte(hdr("VendGrp")), CInt(hdr("Vendor")))
        For Each c As DataRow In dtC.Rows
            If IsDBNull(c("ExpDate")) OrElse CDate(c("ExpDate")) < Date.Today Then
                reasons.Add(c("CompCode").ToString() & " insurance expired.")
            End If
        Next

        Return reasons.Count = 0
    End Function

    '==========================================================================
    ' STATUS TRANSITIONS (mirror of trg_APInvHdr_u - keep in sync!!)
    '==========================================================================
    Public Function IsTransitionAllowed(ByVal sFrom As String, ByVal sTo As String) As Boolean
        Select Case sFrom
            Case STAT_OPEN
                Return sTo = STAT_PENDAPPR OrElse sTo = STAT_APPROVED OrElse sTo = STAT_VOID
            Case STAT_PENDAPPR
                Return sTo = STAT_APPROVED OrElse sTo = STAT_OPEN OrElse sTo = STAT_VOID
            Case STAT_APPROVED
                Return sTo = STAT_HOLD OrElse sTo = STAT_PAID OrElse sTo = STAT_VOID
            Case STAT_HOLD
                Return sTo = STAT_APPROVED OrElse sTo = STAT_VOID
            Case Else
                Return False ' paid / void are final
        End Select
    End Function

    '==========================================================================
    ' early pay discount -- replaced by APPayTerms in 2011, left in for reference
    '==========================================================================
    Public Function ApplyEarlyPayDiscount(ByVal GrossAmt As Decimal, ByVal InvDate As Date, _
                                          ByVal PayDate As Date) As Decimal
        ' 2% 10 net 30
        If DateDiff(DateInterval.Day, InvDate, PayDate) <= 10 Then
            Return RoundAmt(GrossAmt * 0.02D)
        End If
        Return 0D
    End Function

End Class
