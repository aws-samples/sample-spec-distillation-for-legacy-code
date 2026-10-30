' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmJCCOEntry.vb
' Change order entry. Create a CO against a job, add cost items, save as
' draft, submit for approval, void, or reopen a rejected CO.
'   08/2006 DWH  created
'   02/2010 MJR  approval limits, subcontract COs
'   03/2012 MJR  Save & Approve for minor COs (PM request)
'   09/2014 TKB  show field app source
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Windows.Forms

Public Class frmJCCOEntry
    Inherits System.Windows.Forms.Form

    Friend WithEvents txtJob As New TextBox
    Friend WithEvents lblJobDesc As New Label
    Friend WithEvents txtCONum As New TextBox
    Friend WithEvents txtDesc As New TextBox
    Friend WithEvents cboType As New ComboBox
    Friend WithEvents cboReason As New ComboBox
    Friend WithEvents cboSL As New ComboBox
    Friend WithEvents lblSLRemain As New Label
    Friend WithEvents dtpReqDate As New DateTimePicker
    Friend WithEvents txtReqBy As New TextBox
    Friend WithEvents txtMarkup As New TextBox
    Friend WithEvents txtDaysExt As New TextBox
    Friend WithEvents grdItems As New DataGridView
    Friend WithEvents lblCost As New Label
    Friend WithEvents lblMarkup As New Label
    Friend WithEvents lblTax As New Label
    Friend WithEvents lblTotal As New Label
    Friend WithEvents lblExec As New Label
    Friend WithEvents lblStatus As New Label
    Friend WithEvents btnSave As New Button
    Friend WithEvents btnSubmit As New Button
    Friend WithEvents btnSaveApprove As New Button
    Friend WithEvents btnVoid As New Button
    Friend WithEvents btnReopen As New Button
    Friend WithEvents txtVoidReason As New TextBox

    Private m_BL As clsJCCOBL
    Private m_DAL As clsJCCODAL
    Private m_Parms As DataRow
    Private m_Job As DataRow
    Private m_dtHdr As DataTable
    Private m_dtItems As DataTable
    Private m_CONum As String = ""
    Private m_Status As String = CO_DRAFT

    ' preview amounts shown on screen
    Private m_Cost As Decimal, m_Mk As Decimal, m_Tax As Decimal, m_Total As Decimal

    Private Sub frmJCCOEntry_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_DAL = New clsJCCODAL
        m_BL = New clsJCCOBL(m_DAL)
        m_Parms = m_DAL.GetCoParms(g_JCCo)
        Me.Text = "JC Change Order Entry"

        cboType.Items.AddRange(New Object() {"O - Owner", "S - Subcontract", "I - Internal"})
        cboType.SelectedIndex = 0
        cboReason.DataSource = m_DAL.GetReasons()
        cboReason.DisplayMember = "Description"
        cboReason.ValueMember = "ReasonCode"
        txtDesc.MaxLength = MAX_DESC_LEN
        txtDaysExt.Text = "0"

        If m_Parms IsNot Nothing Then
            txtMarkup.Text = CDec(m_Parms("DfltMarkupPct")).ToString("0.000")
        Else
            txtMarkup.Text = DFLT_MARKUP_PCT.ToString("0.000")
        End If

        lblExec.Text = "Change orders over " & Format(CO_PM_LIMIT, "$#,##0") & " need executive approval."
        lblExec.Visible = False
        btnSaveApprove.Text = "Save && Approve"
        btnSaveApprove.Visible = False
        BuildItemTable()
        SetButtons()
    End Sub

    Private Sub BuildItemTable()
        m_dtItems = New DataTable
        m_dtItems.Columns.Add("Phase", GetType(String))
        m_dtItems.Columns.Add("CostType", GetType(String))
        m_dtItems.Columns.Add("Units", GetType(Decimal))
        m_dtItems.Columns.Add("UM", GetType(String))
        m_dtItems.Columns.Add("UnitCost", GetType(Decimal))
        m_dtItems.Columns.Add("Amount", GetType(Decimal))
        m_dtItems.Columns.Add("TaxYN", GetType(String))
        m_dtItems.Columns("UM").DefaultValue = "LS"
        m_dtItems.Columns("TaxYN").DefaultValue = "N"
        m_dtItems.Columns("Amount").ReadOnly = False
        grdItems.DataSource = m_dtItems
        grdItems.Columns("Amount").ReadOnly = True
    End Sub

    '--------------------------------------------------------------------------
    ' job / type / subcontract
    '--------------------------------------------------------------------------
    Private Sub txtJob_Validating(ByVal sender As Object, ByVal e As System.ComponentModel.CancelEventArgs) Handles txtJob.Validating
        If txtJob.Text.Trim() = "" Then Return
        m_Job = m_DAL.GetJob(g_JCCo, txtJob.Text.Trim())
        If m_Job Is Nothing Then
            MessageBox.Show("Job not found.", Me.Text)
            e.Cancel = True
            Return
        End If
        lblJobDesc.Text = CStr(m_Job("Description"))
        Dim js As Integer = CInt(m_Job("JobStatus"))
        If js <> JOB_OPEN Then
            MessageBox.Show("Job is closed. Change orders cannot be added.", Me.Text)
            e.Cancel = True
            Return
        End If
        cboSL.DataSource = m_DAL.GetSLsForJob(g_JCCo, CStr(m_Job("Job")))
        cboSL.DisplayMember = "SL"
        cboSL.ValueMember = "SL"
        RecalcPreview()
    End Sub

    Private Function SelType() As String
        Return CStr(cboType.SelectedItem).Substring(0, 1)
    End Function

    Private Sub cboType_SelectedIndexChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles cboType.SelectedIndexChanged
        cboSL.Enabled = (SelType() = COTYPE_SUB)
        lblSLRemain.Visible = cboSL.Enabled
    End Sub

    Private Sub cboSL_SelectedIndexChanged(ByVal sender As Object, ByVal e As System.EventArgs) Handles cboSL.SelectedIndexChanged
        If cboSL.SelectedValue Is Nothing OrElse m_Parms Is Nothing Then Return
        Dim sl As DataRow = m_DAL.GetSL(g_JCCo, CStr(cboSL.SelectedValue))
        If sl Is Nothing Then Return
        lblSLRemain.Text = "CO capacity remaining: " & _
            Format(m_BL.SubCapRemaining(sl, CDec(m_Parms("DfltMaxCOPct"))), "$#,##0.00")
    End Sub

    Private Sub txtMarkup_Validating(ByVal sender As Object, ByVal e As System.ComponentModel.CancelEventArgs) Handles txtMarkup.Validating
        Dim pct As Decimal
        If Not Decimal.TryParse(txtMarkup.Text, pct) OrElse pct < 0D OrElse pct > MAX_MARKUP_PCT Then
            MessageBox.Show("Markup must be between 0 and " & MAX_MARKUP_PCT & "%.", Me.Text)
            e.Cancel = True
            Return
        End If
        RecalcPreview()
    End Sub

    '--------------------------------------------------------------------------
    ' items / totals preview
    '--------------------------------------------------------------------------
    Private Sub grdItems_CellEndEdit(ByVal sender As Object, ByVal e As DataGridViewCellEventArgs) Handles grdItems.CellEndEdit
        Dim r As DataRow = CType(grdItems.Rows(e.RowIndex).DataBoundItem, DataRowView).Row
        Dim u As Decimal = IIf(IsDBNull(r("Units")), 0D, r("Units"))
        Dim uc As Decimal = IIf(IsDBNull(r("UnitCost")), 0D, r("UnitCost"))
        r("Amount") = Math.Round(u * uc, 2)
        RecalcPreview()
    End Sub

    Private Sub RecalcPreview()
        Dim pct As Decimal = 0D
        Decimal.TryParse(txtMarkup.Text, pct)
        Dim rate As Decimal = 0D
        If m_Job IsNot Nothing Then rate = CDec(m_Job("TaxRate"))

        m_Cost = 0D : m_Mk = 0D : m_Tax = 0D
        For Each r As DataRow In m_dtItems.Rows
            If r.RowState = DataRowState.Deleted Then Continue For
            Dim amt As Decimal = IIf(IsDBNull(r("Amount")), 0D, r("Amount"))
            m_Cost += amt
            m_Mk += Math.Round(amt * pct / 100D, 2, MidpointRounding.AwayFromZero)
            If CStr(r("TaxYN")) = "Y" Then
                m_Tax += Math.Round(amt * rate, 2, MidpointRounding.AwayFromZero)
            End If
        Next
        m_Total = m_Cost + m_Mk + m_Tax

        lblCost.Text = Format(m_Cost, "$#,##0.00")
        lblMarkup.Text = Format(m_Mk, "$#,##0.00")
        lblTax.Text = Format(m_Tax, "$#,##0.00")
        lblTotal.Text = Format(m_Total, "$#,##0.00")
        lblExec.Visible = (m_Total > CO_PM_LIMIT)
        btnSaveApprove.Visible = (m_Status = CO_DRAFT) AndAlso m_BL.CanQuickApprove(g_UserRole, m_Total)
    End Sub

    '--------------------------------------------------------------------------
    ' save
    '--------------------------------------------------------------------------
    Private Function BuildHdrRow() As DataRow
        m_dtHdr = New DataTable
        For Each c As String In New String() {"JCCo", "Job", "CONum", "Description", "COType", "ReasonCode", _
                                              "SL", "ReqDate", "ReqBy", "MarkupPct", "DaysExt"}
            m_dtHdr.Columns.Add(c, GetType(Object))
        Next
        Dim r As DataRow = m_dtHdr.NewRow()
        r("JCCo") = g_JCCo
        r("Job") = txtJob.Text.Trim()
        r("CONum") = m_CONum
        r("Description") = txtDesc.Text.Trim()
        r("COType") = SelType()
        r("ReasonCode") = cboReason.SelectedValue
        r("SL") = IIf(SelType() = COTYPE_SUB AndAlso cboSL.SelectedValue IsNot Nothing, cboSL.SelectedValue, DBNull.Value)
        r("ReqDate") = dtpReqDate.Value.Date
        r("ReqBy") = IIf(txtReqBy.Text.Trim() = "", g_UserName, txtReqBy.Text.Trim())
        r("MarkupPct") = CDec(Val(txtMarkup.Text))
        r("DaysExt") = CInt(Val(txtDaysExt.Text))
        Return r
    End Function

    Private Function SaveCO() As Boolean
        RecalcPreview()
        Dim hdr As DataRow = BuildHdrRow()
        Dim ok As Boolean = m_BL.ValidateHeader(hdr, m_Job, m_Total)
        ok = m_BL.ValidateItems(m_dtItems) AndAlso ok
        If ok AndAlso SelType() = COTYPE_SUB Then
            Dim sl As DataRow = m_DAL.GetSL(g_JCCo, CStr(hdr("SL")))
            If sl IsNot Nothing Then ok = m_BL.CheckSubCap(sl, m_Cost, CDec(m_Parms("DfltMaxCOPct")))
        End If
        If Not ok Then
            MessageBox.Show(m_BL.ErrorText, Me.Text)
            Return False
        End If

        If m_CONum = "" Then
            m_CONum = m_DAL.GetNextCONum(g_JCCo, CStr(hdr("Job")))
            hdr("CONum") = m_CONum
            If Not m_DAL.InsertHdr(hdr) Then
                MessageBox.Show("Save failed: " & m_DAL.LastError, Me.Text)
                m_CONum = ""
                Return False
            End If
        End If
        If Not m_DAL.ReplaceItems(g_JCCo, CStr(hdr("Job")), m_CONum, m_dtItems) Then
            MessageBox.Show("Item save failed: " & m_DAL.LastError, Me.Text)
            Return False
        End If
        m_DAL.SaveTotals(g_JCCo, CStr(hdr("Job")), m_CONum, m_Cost, m_Mk, m_Tax, m_Total)
        txtCONum.Text = m_CONum
        Return True
    End Function

    Private Sub btnSave_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnSave.Click
        If SaveCO() Then
            lblStatus.Text = "Saved as draft."
        End If
    End Sub

    Private Sub btnSubmit_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnSubmit.Click
        If Not SaveCO() Then Return
        Dim msg As String = ""
        Dim rc As Integer = m_DAL.SubmitCO(g_JCCo, txtJob.Text.Trim(), m_CONum, msg)
        Select Case rc
            Case 0
                m_Status = CO_PENDING
                lblStatus.Text = "Submitted for approval."
            Case 99
                MessageBox.Show("Database error: " & msg, Me.Text)
            Case Else
                MessageBox.Show(msg, Me.Text)
        End Select
        SetButtons()
    End Sub

    ' minor COs - PM saves and approves in one step (MJR 03/2012)
    Private Sub btnSaveApprove_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnSaveApprove.Click
        If Not m_BL.CanQuickApprove(g_UserRole, m_Total) Then Return
        If Not SaveCO() Then Return
        If m_DAL.SetStatus(g_JCCo, txtJob.Text.Trim(), m_CONum, CO_APPROVED) Then
            m_Status = CO_APPROVED
            lblStatus.Text = "Saved and approved."
        Else
            MessageBox.Show("Approve failed: " & m_DAL.LastError, Me.Text)
        End If
        SetButtons()
    End Sub

    Private Sub btnVoid_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnVoid.Click
        If m_CONum = "" Then Return
        If Not m_BL.CanVoid(m_Status, txtVoidReason.Text) Then
            MessageBox.Show(m_BL.ErrorText, Me.Text)
            Return
        End If
        If MessageBox.Show("Void change order " & m_CONum & "?", Me.Text, MessageBoxButtons.YesNo) = DialogResult.Yes Then
            If m_DAL.VoidCO(g_JCCo, txtJob.Text.Trim(), m_CONum, txtVoidReason.Text.Trim()) Then
                m_Status = CO_VOID
                lblStatus.Text = "Void."
            Else
                MessageBox.Show(m_DAL.LastError, Me.Text)
            End If
        End If
        SetButtons()
    End Sub

    Private Sub btnReopen_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnReopen.Click
        If m_Status <> CO_REJECTED Then Return
        If m_DAL.ReopenCO(g_JCCo, txtJob.Text.Trim(), m_CONum) Then
            m_Status = CO_DRAFT
            lblStatus.Text = "Reopened as draft."
        End If
        SetButtons()
    End Sub

    Private Sub SetButtons()
        btnSave.Enabled = (m_Status = CO_DRAFT)
        btnSubmit.Enabled = (m_Status = CO_DRAFT) AndAlso g_UserRole <> ""
        btnVoid.Enabled = (m_CONum <> "") AndAlso m_Status <> CO_VOID AndAlso m_Status <> CO_INTERFACED
        btnReopen.Enabled = (m_Status = CO_REJECTED)
        grdItems.ReadOnly = (m_Status <> CO_DRAFT)
        If m_Status <> CO_DRAFT Then btnSaveApprove.Visible = False
    End Sub

End Class
