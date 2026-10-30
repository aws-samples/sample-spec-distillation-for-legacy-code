' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmAPInvoiceEntry.vb
' AP Invoice Entry. Header + lines grid. Save inserts rows then calls
' usp_APInvPost which decides Approved vs Pending Approval.
'
'   03/2004 RDM  created
'   11/2009 KLT  dup check prompt / override
'   06/2013 PSV  lien waiver checkbox
'   02/2016 RDM  retg calc per line (bug 2231)
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Collections.Generic
Imports System.Windows.Forms

Public Class frmAPInvoiceEntry

    Private m_BL As clsAPInvoiceBL
    Private m_dtLines As DataTable
    Private m_Mth As Date
    Private m_APTrans As Integer = 0          ' 0 = new
    Private m_Status As String = STAT_OPEN
    Private m_VendPayTerms As Object = Nothing
    Private m_bLoading As Boolean = False

    Private Sub frmAPInvoiceEntry_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_bLoading = True
        Try
            m_BL = New clsAPInvoiceBL
            BuildLinesTable()
            grdLines.DataSource = m_dtLines
            dtpMth.Value = FirstOfMonth(Date.Today)
            dtpInvDate.Value = Date.Today
            dtpDueDate.Value = Date.Today.AddDays(DFLT_PAY_TERMS)
            lblStatus.Text = StatusText(STAT_OPEN)
            btnVoid.Enabled = False
        Catch ex As Exception
            MsgBox("Error loading AP Invoice Entry: " & ex.Message, MsgBoxStyle.Critical)
        Finally
            m_bLoading = False
        End Try
    End Sub

    Private Sub BuildLinesTable()
        m_dtLines = New DataTable("Lines")
        m_dtLines.Columns.Add("APLine", GetType(Short))
        m_dtLines.Columns.Add("LineType", GetType(Integer))
        m_dtLines.Columns.Add("Job", GetType(String))
        m_dtLines.Columns.Add("Phase", GetType(String))
        m_dtLines.Columns.Add("GLAcct", GetType(String))
        m_dtLines.Columns.Add("Amount", GetType(Decimal))
        m_dtLines.Columns.Add("TaxAmt", GetType(Decimal))
        m_dtLines.Columns.Add("RetgPct", GetType(Decimal))
        m_dtLines.Columns.Add("RetgAmt", GetType(Decimal))
        m_dtLines.Columns.Add("Descr", GetType(String))
        m_dtLines.Columns("LineType").DefaultValue = LT_JOB
        m_dtLines.Columns("Amount").DefaultValue = 0D
        m_dtLines.Columns("TaxAmt").DefaultValue = 0D
        m_dtLines.Columns("RetgPct").DefaultValue = 0D
        m_dtLines.Columns("RetgAmt").DefaultValue = 0D
        AddHandler m_dtLines.ColumnChanged, AddressOf Lines_ColumnChanged
        AddHandler m_dtLines.TableNewRow, AddressOf Lines_NewRow
    End Sub

    '--------------------------------------------------------------------------
    ' vendor
    '--------------------------------------------------------------------------
    Private Sub txtVendor_Validated(ByVal sender As Object, ByVal e As System.EventArgs) Handles txtVendor.Validated
        If Trim(txtVendor.Text) = "" Then Exit Sub
        Dim v As DataRow = m_BL.DAL.GetVendor(CByte(Val(txtVendGrp.Text)), CInt(Val(txtVendor.Text)))
        If v Is Nothing Then
            lblVendName.Text = "** not found **"
            m_VendPayTerms = Nothing
            Exit Sub
        End If
        lblVendName.Text = v("Name").ToString()
        m_VendPayTerms = v("PayTerms")
        dtpDueDate.Value = m_BL.ComputeDueDate(dtpInvDate.Value, m_VendPayTerms)
        ' 1099 vendors - TIN is checked at post, nothing to do here
    End Sub

    Private Sub dtpInvDate_ValueChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles dtpInvDate.ValueChanged
        If m_bLoading Then Exit Sub
        dtpDueDate.Value = m_BL.ComputeDueDate(dtpInvDate.Value, m_VendPayTerms)
    End Sub

    Private Sub dtpMth_ValueChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles dtpMth.ValueChanged
        If m_bLoading Then Exit Sub
        m_bLoading = True
        dtpMth.Value = FirstOfMonth(dtpMth.Value)
        m_bLoading = False
    End Sub

    Private Sub txtGrossAmt_Validated(ByVal sender As Object, ByVal e As System.EventArgs) Handles txtGrossAmt.Validated
        Dim g As Decimal = CDec(Val(txtGrossAmt.Text))
        txtGrossAmt.Text = Format(m_BL.RoundAmt(g), "0.00")
        lblApprNeeded.Visible = m_BL.RequiresApproval(g)
    End Sub

    '--------------------------------------------------------------------------
    ' lines grid
    '--------------------------------------------------------------------------
    Private Sub Lines_NewRow(ByVal sender As Object, ByVal e As DataTableNewRowEventArgs)
        Dim maxLn As Short = 0
        For Each r As DataRow In m_dtLines.Rows
            If r.RowState <> DataRowState.Deleted AndAlso CShort(r("APLine")) > maxLn Then maxLn = CShort(r("APLine"))
        Next
        e.Row("APLine") = maxLn + 1
        If Trim(txtVendor.Text) <> "" Then
            e.Row("RetgPct") = m_BL.GetDfltRetgPct(CByte(Val(txtVendGrp.Text)), CInt(Val(txtVendor.Text)))
        End If
        If Trim(txtJob.Text) <> "" Then e.Row("Job") = Trim(txtJob.Text)
    End Sub

    Private Sub Lines_ColumnChanged(ByVal sender As Object, ByVal e As DataColumnChangeEventArgs)
        If m_bLoading Then Exit Sub
        Select Case e.Column.ColumnName
            Case "Amount", "RetgPct", "LineType"
                m_bLoading = True
                Try
                    Dim lt As Integer = CInt(e.Row("LineType"))
                    Dim amt As Decimal = m_BL.RoundAmt(CDec(e.Row("Amount")))
                    e.Row("Amount") = amt
                    If lt <> LT_JOB Then
                        e.Row("RetgPct") = 0D  ' expense lines never carry retainage
                    End If
                    e.Row("RetgAmt") = m_BL.ComputeLineRetg(lt, amt, CDec(e.Row("RetgPct")))
                    txtRetgAmt.Text = Format(m_BL.ComputeHdrRetg(m_dtLines), "0.00")
                Finally
                    m_bLoading = False
                End Try
        End Select
    End Sub

    '--------------------------------------------------------------------------
    ' save
    '--------------------------------------------------------------------------
    Private Sub btnSave_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnSave.Click
        Dim errs As List(Of String) = Nothing
        Dim vg As Byte = CByte(Val(txtVendGrp.Text))
        Dim vend As Integer = CInt(Val(txtVendor.Text))
        Dim sRef As String = m_BL.NormalizeAPRef(txtAPRef.Text)
        Dim gross As Decimal = m_BL.RoundAmt(CDec(Val(txtGrossAmt.Text)))
        Dim tax As Decimal = m_BL.RoundAmt(CDec(Val(txtTaxAmt.Text)))
        Dim mth As Date = FirstOfMonth(dtpMth.Value)

        If m_APTrans <> 0 Then
            MsgBox("Posted invoices cannot be changed. Void and re-enter.", MsgBoxStyle.Exclamation)
            Exit Sub
        End If

        If Not m_BL.ValidateHeader(vg, vend, sRef, dtpInvDate.Value, mth, gross, errs) Then
            MsgBox(String.Join(vbCrLf, errs.ToArray()), MsgBoxStyle.Exclamation, "Invoice Header")
            Exit Sub
        End If
        If Not m_BL.ValidateLines(m_dtLines, gross, errs) Then
            MsgBox(String.Join(vbCrLf, errs.ToArray()), MsgBoxStyle.Exclamation, "Invoice Lines")
            Exit Sub
        End If

        ' job invoice over lien waiver threshold w/o waiver - allowed to enter, will hold at approval
        If Trim(txtJob.Text) <> "" AndAlso Not chkLienWvr.Checked AndAlso gross >= 5000D Then
            If MsgBox("No lien waiver on file. Invoice will be placed on hold until received. Continue?", _
                      MsgBoxStyle.YesNo Or MsgBoxStyle.Question) = MsgBoxResult.No Then Exit Sub
        End If

        '--- duplicate check (KLT 2009)
        Dim sDupOvrd As String = ""
        Dim dtDup As DataTable = m_BL.FindDuplicates(vg, vend, sRef, gross, dtpInvDate.Value)
        If dtDup.Rows.Count > 0 Then
            Dim r0 As DataRow = dtDup.Rows(0)
            Dim sMsg As String = "Possible duplicate of invoice " & r0("APRef").ToString() & _
                                 " (Mth " & Format(CDate(r0("Mth")), "MM/yy") & " Trans " & r0("APTrans").ToString() & _
                                 ", " & Format(CDate(r0("InvDate")), "MM/dd/yyyy") & ")."
            If Not m_BL.CanOverrideDup(g_UserRole) Then
                MsgBox(sMsg & vbCrLf & "A Controller must override duplicates.", MsgBoxStyle.Exclamation)
                Exit Sub
            End If
            If MsgBox(sMsg & vbCrLf & "Override and post anyway?", MsgBoxStyle.YesNo Or MsgBoxStyle.Exclamation) _
               = MsgBoxResult.No Then Exit Sub
            sDupOvrd = g_UserId
        End If

        '--- insert
        Dim dueDt As Date = dtpDueDate.Value.Date
        Dim nTerms As Integer = DFLT_PAY_TERMS
        If m_VendPayTerms IsNot Nothing AndAlso Not IsDBNull(m_VendPayTerms) Then nTerms = CInt(m_VendPayTerms)

        Dim t As Integer = m_BL.DAL.InsertInvoiceHdr(g_APCo, mth, FirstOfMonth(Date.Today), vg, vend, sRef, _
                                dtpInvDate.Value.Date, dueDt, nTerms, txtDescr.Text, gross, tax, _
                                IIf(chkLienWvr.Checked, "Y", "N"), Trim(txtJob.Text))
        If t < 0 Then
            MsgBox("Insert failed: " & m_BL.DAL.LastError, MsgBoxStyle.Critical)
            Exit Sub
        End If

        For Each r As DataRow In m_dtLines.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            If Not m_BL.DAL.InsertInvLine(g_APCo, mth, t, CShort(r("APLine")), CInt(r("LineType")), _
                     r("Job").ToString(), r("Phase").ToString(), r("GLAcct").ToString(), CDec(r("Amount")), _
                     CDec(r("TaxAmt")), CDec(r("RetgPct")), CDec(r("RetgAmt")), r("Descr").ToString()) Then
                MsgBox("Line insert failed: " & m_BL.DAL.LastError, MsgBoxStyle.Critical)
                ' TODO: header is orphaned here. clean up job runs nightly (RDM)
                Exit Sub
            End If
        Next

        '--- post (proc decides P vs A, and re-checks everything it cares about)
        Dim sPostMsg As String = ""
        Dim rc As Integer = m_BL.DAL.PostInvoice(g_APCo, mth, t, sDupOvrd, sPostMsg)
        If rc <> 0 Then
            MsgBox("Post failed (" & rc & "): " & sPostMsg, MsgBoxStyle.Critical)
            Exit Sub
        End If

        m_Mth = mth
        m_APTrans = t
        Dim hdr As DataRow = m_BL.DAL.GetInvoice(g_APCo, mth, t)
        If hdr IsNot Nothing Then m_Status = hdr("Status").ToString()
        lblStatus.Text = StatusText(m_Status)
        btnVoid.Enabled = True
        MsgBox("Invoice posted as trans " & t & " (" & StatusText(m_Status) & ").", MsgBoxStyle.Information)
    End Sub

    '--------------------------------------------------------------------------
    ' void
    '--------------------------------------------------------------------------
    Private Sub btnVoid_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnVoid.Click
        If m_APTrans = 0 Then Exit Sub
        If Not m_BL.IsTransitionAllowed(m_Status, STAT_VOID) Then
            MsgBox("A " & StatusText(m_Status) & " invoice cannot be voided.", MsgBoxStyle.Exclamation)
            Exit Sub
        End If
        Dim sReason As String = InputBox("Void reason (required):", "Void Invoice")
        If Trim(sReason) = "" Then
            MsgBox("A void reason is required.", MsgBoxStyle.Exclamation)
            Exit Sub
        End If
        If m_BL.DAL.VoidInvoice(g_APCo, m_Mth, m_APTrans, Trim(sReason)) Then
            m_Status = STAT_VOID
            lblStatus.Text = StatusText(m_Status)
            btnVoid.Enabled = False
        Else
            MsgBox("Void failed: " & m_BL.DAL.LastError, MsgBoxStyle.Critical)
        End If
    End Sub

    Private Sub btnClose_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnClose.Click
        Me.Close()
    End Sub

End Class
