' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' frmJCSubcontractMaint.vb
' Subcontract maintenance - header fields, CO limit, insurance expiry, status.
' Shows the change orders written against the subcontract.
'   02/2010 MJR  created (split out of SL entry)
'   11/2015 TKB  insurance expiry
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Windows.Forms

Public Class frmJCSubcontractMaint
    Inherits System.Windows.Forms.Form

    Friend WithEvents txtSL As New TextBox
    Friend WithEvents txtDesc As New TextBox
    Friend WithEvents lblJob As New Label
    Friend WithEvents lblVendor As New Label
    Friend WithEvents lblOrig As New Label
    Friend WithEvents lblCur As New Label
    Friend WithEvents txtRetgPct As New TextBox
    Friend WithEvents txtMaxCOPct As New TextBox
    Friend WithEvents lblCapRemain As New Label
    Friend WithEvents dtpInsExp As New DateTimePicker
    Friend WithEvents cboStatus As New ComboBox
    Friend WithEvents grdCOs As New DataGridView
    Friend WithEvents btnSave As New Button

    Private m_DAL As clsJCCODAL
    Private m_Parms As DataRow
    Private m_SL As DataRow

    Private Sub frmJCSubcontractMaint_Load(ByVal sender As Object, ByVal e As System.EventArgs) Handles MyBase.Load
        m_DAL = New clsJCCODAL
        m_Parms = m_DAL.GetCoParms(g_JCCo)
        Me.Text = "Subcontract Maintenance"
        cboStatus.Items.AddRange(New Object() {"0 - Open", "1 - Complete", "2 - Closed"})
        btnSave.Enabled = False
    End Sub

    Private Sub txtSL_Validating(ByVal sender As Object, ByVal e As System.ComponentModel.CancelEventArgs) Handles txtSL.Validating
        If txtSL.Text.Trim() = "" Then Return
        m_SL = m_DAL.GetSL(g_JCCo, txtSL.Text.Trim())
        If m_SL Is Nothing Then
            MessageBox.Show("Subcontract not found.", Me.Text)
            e.Cancel = True
            Return
        End If
        txtDesc.Text = IIf(IsDBNull(m_SL("Description")), "", m_SL("Description"))
        lblJob.Text = CStr(m_SL("Job"))
        lblVendor.Text = CStr(m_SL("Vendor"))
        lblOrig.Text = Format(CDec(m_SL("OrigCost")), "$#,##0.00")
        lblCur.Text = Format(CDec(m_SL("CurCost")), "$#,##0.00")
        txtRetgPct.Text = CDec(m_SL("RetgPct")).ToString("0.000")
        txtMaxCOPct.Text = IIf(IsDBNull(m_SL("MaxCOPct")), "", CDec(m_SL("MaxCOPct")).ToString("0.000"))
        If Not IsDBNull(m_SL("InsExpDate")) Then dtpInsExp.Value = CDate(m_SL("InsExpDate"))
        cboStatus.SelectedIndex = CInt(m_SL("SLStatus"))
        grdCOs.DataSource = m_DAL.GetSLCOSummary(g_JCCo, CStr(m_SL("SL")))
        ShowCapacity()
        btnSave.Enabled = True
    End Sub

    ' remaining CO capacity = cap - change orders already in the subcontract
    Private Sub ShowCapacity()
        Dim pct As Decimal
        If txtMaxCOPct.Text.Trim() = "" Then
            pct = CDec(m_Parms("DfltMaxCOPct"))
        Else
            pct = CDec(Val(txtMaxCOPct.Text))
        End If
        Dim orig As Decimal = CDec(m_SL("OrigCost"))
        Dim used As Decimal = CDec(m_SL("CurCost")) - orig
        lblCapRemain.Text = Format(Math.Round(orig * pct / 100D, 2) - used, "$#,##0.00")
    End Sub

    Private Sub txtMaxCOPct_Leave(ByVal sender As Object, ByVal e As System.EventArgs) Handles txtMaxCOPct.Leave
        If m_SL IsNot Nothing Then ShowCapacity()
    End Sub

    Private Sub btnSave_Click(ByVal sender As Object, ByVal e As System.EventArgs) Handles btnSave.Click
        If m_SL Is Nothing Then Return

        Dim retg As Decimal = CDec(Val(txtRetgPct.Text))
        If retg < 0D OrElse retg > 10D Then
            MessageBox.Show("Retainage must be between 0 and 10%.", Me.Text)
            Return
        End If

        Dim maxCO As Object = DBNull.Value
        If txtMaxCOPct.Text.Trim() <> "" Then
            Dim p As Decimal = CDec(Val(txtMaxCOPct.Text))
            If p < 0D OrElse p > 50D Then
                MessageBox.Show("CO limit must be between 0 and 50% of the original subcontract.", Me.Text)
                Return
            End If
            maxCO = p
        End If

        Dim newStat As Integer = cboStatus.SelectedIndex
        If newStat = 2 AndAlso m_DAL.CountOpenCOsForSL(g_JCCo, CStr(m_SL("SL"))) > 0 Then
            MessageBox.Show("Subcontract has open change orders and cannot be closed.", Me.Text)
            Return
        End If

        m_SL("Description") = txtDesc.Text.Trim()
        m_SL("RetgPct") = retg
        m_SL("MaxCOPct") = maxCO
        m_SL("InsExpDate") = dtpInsExp.Value.Date
        m_SL("SLStatus") = newStat
        If m_DAL.SaveSL(m_SL) Then
            MessageBox.Show("Saved.", Me.Text)
        Else
            MessageBox.Show("Save failed: " & m_DAL.LastError, Me.Text)
        End If
    End Sub

End Class
