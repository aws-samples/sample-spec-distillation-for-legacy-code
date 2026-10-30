' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
<Global.Microsoft.VisualBasic.CompilerServices.DesignerGenerated()> _
Partial Class frmAPInvoiceEntry
    Inherits System.Windows.Forms.Form

    'Form overrides dispose to clean up the component list.
    <System.Diagnostics.DebuggerNonUserCode()> _
    Protected Overrides Sub Dispose(ByVal disposing As Boolean)
        Try
            If disposing AndAlso components IsNot Nothing Then
                components.Dispose()
            End If
        Finally
            MyBase.Dispose(disposing)
        End Try
    End Sub

    'Required by the Windows Form Designer
    Private components As System.ComponentModel.IContainer

    'NOTE: The following procedure is required by the Windows Form Designer
    'It can be modified using the Windows Form Designer.
    'Do not modify it using the code editor.
    <System.Diagnostics.DebuggerStepThrough()> _
    Private Sub InitializeComponent()
        Me.lblVendor = New System.Windows.Forms.Label()
        Me.txtVendGrp = New System.Windows.Forms.TextBox()
        Me.txtVendor = New System.Windows.Forms.TextBox()
        Me.lblVendName = New System.Windows.Forms.Label()
        Me.lblAPRef = New System.Windows.Forms.Label()
        Me.txtAPRef = New System.Windows.Forms.TextBox()
        Me.lblInvDate = New System.Windows.Forms.Label()
        Me.dtpInvDate = New System.Windows.Forms.DateTimePicker()
        Me.lblDueDate = New System.Windows.Forms.Label()
        Me.dtpDueDate = New System.Windows.Forms.DateTimePicker()
        Me.lblMth = New System.Windows.Forms.Label()
        Me.dtpMth = New System.Windows.Forms.DateTimePicker()
        Me.lblGross = New System.Windows.Forms.Label()
        Me.txtGrossAmt = New System.Windows.Forms.TextBox()
        Me.txtTaxAmt = New System.Windows.Forms.TextBox()
        Me.lblRetg = New System.Windows.Forms.Label()
        Me.txtRetgAmt = New System.Windows.Forms.TextBox()
        Me.txtDescr = New System.Windows.Forms.TextBox()
        Me.txtJob = New System.Windows.Forms.TextBox()
        Me.chkLienWvr = New System.Windows.Forms.CheckBox()
        Me.grdLines = New System.Windows.Forms.DataGridView()
        Me.btnSave = New System.Windows.Forms.Button()
        Me.btnVoid = New System.Windows.Forms.Button()
        Me.btnClose = New System.Windows.Forms.Button()
        Me.lblStatus = New System.Windows.Forms.Label()
        Me.lblApprNeeded = New System.Windows.Forms.Label()
        CType(Me.grdLines, System.ComponentModel.ISupportInitialize).BeginInit()
        Me.SuspendLayout()
        '
        'txtAPRef
        '
        Me.txtAPRef.CharacterCasing = System.Windows.Forms.CharacterCasing.Upper
        Me.txtAPRef.MaxLength = 15
        Me.txtAPRef.Location = New System.Drawing.Point(110, 40)
        Me.txtAPRef.Name = "txtAPRef"
        '
        'chkLienWvr
        '
        Me.chkLienWvr.Text = "Lien waiver received"
        Me.chkLienWvr.Location = New System.Drawing.Point(420, 140)
        Me.chkLienWvr.Name = "chkLienWvr"
        '
        'lblApprNeeded
        '
        Me.lblApprNeeded.ForeColor = System.Drawing.Color.Firebrick
        Me.lblApprNeeded.Text = "Requires approval (>= $10,000)"
        Me.lblApprNeeded.Visible = False
        Me.lblApprNeeded.Name = "lblApprNeeded"
        '
        '(remaining layout omitted - designer stub)
        '
        Me.ClientSize = New System.Drawing.Size(760, 540)
        Me.Controls.Add(Me.grdLines)
        Me.Controls.Add(Me.txtAPRef)
        Me.Controls.Add(Me.chkLienWvr)
        Me.Controls.Add(Me.lblApprNeeded)
        Me.Name = "frmAPInvoiceEntry"
        Me.Text = "AP Invoice Entry"
        CType(Me.grdLines, System.ComponentModel.ISupportInitialize).EndInit()
        Me.ResumeLayout(False)
        Me.PerformLayout()
    End Sub

    Friend WithEvents lblVendor As System.Windows.Forms.Label
    Friend WithEvents txtVendGrp As System.Windows.Forms.TextBox
    Friend WithEvents txtVendor As System.Windows.Forms.TextBox
    Friend WithEvents lblVendName As System.Windows.Forms.Label
    Friend WithEvents lblAPRef As System.Windows.Forms.Label
    Friend WithEvents txtAPRef As System.Windows.Forms.TextBox
    Friend WithEvents lblInvDate As System.Windows.Forms.Label
    Friend WithEvents dtpInvDate As System.Windows.Forms.DateTimePicker
    Friend WithEvents lblDueDate As System.Windows.Forms.Label
    Friend WithEvents dtpDueDate As System.Windows.Forms.DateTimePicker
    Friend WithEvents lblMth As System.Windows.Forms.Label
    Friend WithEvents dtpMth As System.Windows.Forms.DateTimePicker
    Friend WithEvents lblGross As System.Windows.Forms.Label
    Friend WithEvents txtGrossAmt As System.Windows.Forms.TextBox
    Friend WithEvents txtTaxAmt As System.Windows.Forms.TextBox
    Friend WithEvents lblRetg As System.Windows.Forms.Label
    Friend WithEvents txtRetgAmt As System.Windows.Forms.TextBox
    Friend WithEvents txtDescr As System.Windows.Forms.TextBox
    Friend WithEvents txtJob As System.Windows.Forms.TextBox
    Friend WithEvents chkLienWvr As System.Windows.Forms.CheckBox
    Friend WithEvents grdLines As System.Windows.Forms.DataGridView
    Friend WithEvents btnSave As System.Windows.Forms.Button
    Friend WithEvents btnVoid As System.Windows.Forms.Button
    Friend WithEvents btnClose As System.Windows.Forms.Button
    Friend WithEvents lblStatus As System.Windows.Forms.Label
    Friend WithEvents lblApprNeeded As System.Windows.Forms.Label
End Class
