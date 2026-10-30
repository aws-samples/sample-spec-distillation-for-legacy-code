' Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
'==============================================================================
' clsJCFieldSync.vb
' Field app sync.
'   ExportApproved  - pushes approved / posted change orders to the field
'                     service (POST g_FieldApiUrl, JSON, X-Api-Key header)
'   ProcessRequest  - takes one change order request JSON from the field
'                     service and creates a Draft CO (usp_JCCOFieldImport).
'                     Returns the response JSON.
'   GetStatus       - status lookup by extRef for the field service
'   Called by the JCFieldSync scheduled task (every 15 min).
'   TKB 09/2014 created, schema 2.0
'   TKB 03/2017 schema 2.1 - added requiresExecApproval, daysExtension
'==============================================================================
Option Strict Off
Option Explicit On

Imports System.Data
Imports System.Text
Imports System.Net
Imports System.Globalization
Imports System.Collections.Generic
Imports System.Web.Script.Serialization

Public Class clsJCFieldSync

    Private Const MAX_BATCH As Integer = 50
    Private m_DAL As clsJCCODAL
    Private m_BL As clsJCCOBL
    Private m_Inv As CultureInfo = CultureInfo.InvariantCulture

    Public Sub New()
        m_DAL = New clsJCCODAL
        m_BL = New clsJCCOBL(m_DAL)
    End Sub

    '==========================================================================
    ' outbound
    '==========================================================================
    Public Function ExportApproved() As Integer
        Dim dt As DataTable = m_DAL.GetExportable(g_JCCo)
        Dim sent As Integer = 0
        Dim i As Integer = 0
        While i < dt.Rows.Count
            Dim batch As New List(Of DataRow)
            While i < dt.Rows.Count AndAlso batch.Count < MAX_BATCH
                batch.Add(dt.Rows(i))
                i += 1
            End While
            Dim body As String = BuildExportPayload(batch)
            If PostJson(g_FieldApiUrl, body) Then
                For Each r As DataRow In batch
                    m_DAL.MarkExported(g_JCCo, CStr(r("Job")), CStr(r("CONum")))
                    sent += 1
                Next
            Else
                Exit While      ' try again next run
            End If
        End While
        Return sent
    End Function

    Public Function BuildExportPayload(ByVal rows As List(Of DataRow)) As String
        Dim sb As New StringBuilder
        sb.Append("{""schemaVersion"":""").Append(FIELD_SCHEMA_VER).Append(""",")
        sb.Append("""company"":").Append(g_JCCo).Append(",")
        sb.Append("""changeOrders"":[")
        Dim first As Boolean = True
        For Each r As DataRow In rows
            If Not first Then sb.Append(",")
            first = False
            Dim tot As Decimal = CDec(r("TotalAmt"))
            sb.Append("{")
            sb.Append(JStr("job", r("Job"))).Append(",")
            sb.Append(JStr("coNumber", r("CONum"))).Append(",")
            sb.Append(JStr("extRef", r("ExtRef"))).Append(",")
            sb.Append(JStr("source", IIf(CStr(r("SourceSys")) = "FLD", "FIELD", "ERP"))).Append(",")
            sb.Append(JStr("type", IIf(CStr(r("COType")) = COTYPE_SUB, "SUB", "OWNER"))).Append(",")
            sb.Append(JStr("status", m_BL.FieldStatus(CStr(r("Status"))))).Append(",")
            sb.Append(JStr("description", r("Description"))).Append(",")
            sb.Append(JStr("reasonCode", r("ReasonCode"))).Append(",")
            sb.Append(JStr("subcontract", r("SL"))).Append(",")
            sb.Append(JNum("costAmount", CDec(r("CostAmt")))).Append(",")
            sb.Append(JNum("markupAmount", CDec(r("MarkupAmt")))).Append(",")
            sb.Append(JNum("taxAmount", CDec(r("TaxAmt")))).Append(",")
            sb.Append(JNum("totalAmount", tot)).Append(",")
            sb.Append("""daysExtension"":").Append(CInt(r("DaysExt"))).Append(",")
            sb.Append(JStr("approvedBy", r("ApprBy"))).Append(",")
            sb.Append(JStr("approvedDate", IIf(IsDBNull(r("ApprDate")), DBNull.Value, _
                      CDate(r("ApprDate")).ToString("yyyy-MM-ddTHH:mm:ss", m_Inv)))).Append(",")
            sb.Append("""requiresExecApproval"":").Append(IIf(m_BL.RequiresExecApproval(tot), "true", "false"))
            sb.Append("}")
        Next
        sb.Append("]}")
        Return sb.ToString()
    End Function

    Private Function PostJson(ByVal url As String, ByVal body As String) As Boolean
        Try
            Dim req As HttpWebRequest = CType(WebRequest.Create(url), HttpWebRequest)
            req.Method = "POST"
            req.ContentType = "application/json; charset=utf-8"
            req.Headers.Add("X-Api-Key", g_FieldApiKey)
            req.Timeout = 30000
            Dim bytes() As Byte = Encoding.UTF8.GetBytes(body)
            Using s As IO.Stream = req.GetRequestStream()
                s.Write(bytes, 0, bytes.Length)
            End Using
            Using resp As HttpWebResponse = CType(req.GetResponse(), HttpWebResponse)
                Return resp.StatusCode = HttpStatusCode.OK OrElse resp.StatusCode = HttpStatusCode.Accepted
            End Using
        Catch ex As WebException
            Return False
        End Try
    End Function

    '==========================================================================
    ' inbound
    '   {"extRef":"<guid>", "job":"24-101", "coType":"OWNER|SUB",
    '    "subcontract":"24-101-003", "reasonCode":"UNF",
    '    "description":"...", "requestDate":"2026-09-30",
    '    "requestedBy":"field user", "daysExtension":0,
    '    "items":[{"phase":"...","costType":"L","units":8,"um":"HR",
    '              "unitCost":65.00,"taxable":false}, ...]}
    '==========================================================================
    Public Function ProcessRequest(ByVal json As String) As String
        Dim errs As New List(Of String())
        Dim p As Dictionary(Of String, Object) = Nothing
        Try
            p = New JavaScriptSerializer().Deserialize(Of Dictionary(Of String, Object))(json)
        Catch ex As Exception
            Return Response("", "REJECTED", "", New List(Of String()) From {New String() {"INVALID", "Malformed JSON."}})
        End Try

        Dim extRef As String = GetStr(p, "extRef")
        Dim g As Guid
        If extRef = "" Then
            errs.Add(New String() {"REQUIRED", "extRef is required."})
        ElseIf Not Guid.TryParse(extRef, g) OrElse extRef.Length <> 36 Then
            errs.Add(New String() {"INVALID", "extRef must be a GUID (36 characters)."})
        End If

        If GetStr(p, "job") = "" Then errs.Add(New String() {"REQUIRED", "job is required."})

        Dim desc As String = GetStr(p, "description")
        If desc = "" Then
            errs.Add(New String() {"REQUIRED", "description is required."})
        ElseIf desc.Length > MAX_DESC_LEN Then
            errs.Add(New String() {"INVALID", "description is longer than " & MAX_DESC_LEN & " characters."})
        End If

        Dim sType As String = GetStr(p, "coType").ToUpper()
        Select Case sType
            Case "OWNER" : p("coType") = COTYPE_OWNER
            Case "SUB" : p("coType") = COTYPE_SUB
            Case Else : errs.Add(New String() {"INVALID", "coType must be OWNER or SUB."})
        End Select
        If sType = "SUB" AndAlso GetStr(p, "subcontract") = "" Then
            errs.Add(New String() {"REQUIRED", "subcontract is required for SUB change orders."})
        End If

        If GetStr(p, "reasonCode") = "" Then errs.Add(New String() {"REQUIRED", "reasonCode is required."})

        Dim rq As DateTime
        If Not DateTime.TryParseExact(GetStr(p, "requestDate"), "yyyy-MM-dd", m_Inv, DateTimeStyles.None, rq) Then
            errs.Add(New String() {"INVALID", "requestDate must be yyyy-MM-dd."})
        ElseIf rq.Date > Today Then
            errs.Add(New String() {"INVALID", "requestDate cannot be in the future."})
        Else
            p("requestDate") = rq.Date
        End If

        If GetStr(p, "requestedBy") = "" Then errs.Add(New String() {"REQUIRED", "requestedBy is required."})

        ' items
        Dim items As ArrayList = Nothing
        If p.ContainsKey("items") Then items = TryCast(p("items"), ArrayList)
        Dim costTot As Decimal = 0D
        Dim xml As New StringBuilder("<items>")
        If items Is Nothing OrElse items.Count = 0 Then
            errs.Add(New String() {"REQUIRED", "At least one item is required."})
        ElseIf items.Count > FIELD_MAX_ITEMS Then
            errs.Add(New String() {"LIMIT", "No more than " & FIELD_MAX_ITEMS & " items per request."})
        Else
            Dim n As Integer = 0
            For Each o As Object In items
                n += 1
                Dim it As Dictionary(Of String, Object) = CType(o, Dictionary(Of String, Object))
                Dim ct As String = GetStr(it, "costType").ToUpper()
                Dim u As Decimal = GetDec(it, "units")
                Dim uc As Decimal = GetDec(it, "unitCost")
                If GetStr(it, "phase") = "" Then errs.Add(New String() {"REQUIRED", "items[" & n & "].phase is required."})
                If ct.Length <> 1 OrElse "LMESO".IndexOf(ct) < 0 Then
                    errs.Add(New String() {"INVALID", "items[" & n & "].costType must be L, M, E, S or O."})
                End If
                If u <= 0D Then errs.Add(New String() {"INVALID", "items[" & n & "].units must be greater than 0."})
                If uc < 0D Then errs.Add(New String() {"INVALID", "items[" & n & "].unitCost cannot be negative."})
                costTot += m_BL.CalcItemAmount(u, uc)
                Dim tx As String = IIf(it.ContainsKey("taxable") AndAlso CBool(it("taxable")), "Y", "N")
                Dim um As String = IIf(GetStr(it, "um") = "", "LS", GetStr(it, "um"))
                xml.AppendFormat(m_Inv, "<i ph=""{0}"" ct=""{1}"" u=""{2}"" um=""{3}"" uc=""{4}"" tx=""{5}""/>", _
                                 Security.SecurityElement.Escape(GetStr(it, "phase")), ct, u, _
                                 Security.SecurityElement.Escape(um), uc, tx)
            Next
        End If
        xml.Append("</items>")

        Dim parms As DataRow = m_DAL.GetCoParms(g_JCCo)
        If parms IsNot Nothing AndAlso costTot > CDec(parms("FieldMaxAmt")) Then
            errs.Add(New String() {"LIMIT", "Field requests over " & Format(CDec(parms("FieldMaxAmt")), "$#,##0") & _
                                   " must be entered in the office."})
        End If

        If errs.Count > 0 Then Return Response(extRef, "REJECTED", "", errs)

        Dim coNum As String = ""
        Dim msg As String = ""
        Dim rc As Integer = m_DAL.FieldImport(p, xml.ToString(), coNum, msg)
        Select Case rc
            Case 0
                Return Response(extRef, "ACCEPTED", coNum, errs)
            Case 1
                Return Response(extRef, "DUPLICATE", coNum, errs)
            Case 2
                errs.Add(New String() {"NOT_FOUND", msg})
            Case 3
                errs.Add(New String() {"JOB_CLOSED", msg})
            Case 99
                errs.Add(New String() {"DB_ERROR", "Request could not be saved."})
            Case Else
                errs.Add(New String() {"INVALID", msg})
        End Select
        Return Response(extRef, "REJECTED", "", errs)
    End Function

    Public Function GetStatus(ByVal job As String, ByVal coNum As String) As String
        Dim r As DataRow = m_DAL.GetCO(g_JCCo, job, coNum)
        If r Is Nothing Then Return "{""status"":""UNKNOWN""}"
        Dim sb As New StringBuilder("{")
        sb.Append(JStr("extRef", r("ExtRef"))).Append(",")
        sb.Append(JStr("coNumber", r("CONum"))).Append(",")
        sb.Append(JStr("status", m_BL.FieldStatus(CStr(r("Status"))))).Append(",")
        sb.Append(JStr("rejectReason", r("RejReason")))
        sb.Append("}")
        Return sb.ToString()
    End Function

    '==========================================================================
    ' json helpers
    '==========================================================================
    Private Function Response(ByVal extRef As String, ByVal result As String, ByVal coNum As String, _
                              ByVal errs As List(Of String())) As String
        Dim sb As New StringBuilder("{")
        sb.Append(JStr("extRef", extRef)).Append(",")
        sb.Append(JStr("result", result)).Append(",")
        sb.Append(JStr("coNumber", IIf(coNum = "", DBNull.Value, coNum))).Append(",")
        sb.Append(JStr("status", IIf(result = "REJECTED", DBNull.Value, "RECEIVED"))).Append(",")
        sb.Append("""errors"":[")
        For k As Integer = 0 To errs.Count - 1
            If k > 0 Then sb.Append(",")
            sb.Append("{").Append(JStr("code", errs(k)(0))).Append(",").Append(JStr("message", errs(k)(1))).Append("}")
        Next
        sb.Append("]}")
        Return sb.ToString()
    End Function

    Private Function JStr(ByVal name As String, ByVal v As Object) As String
        If v Is Nothing OrElse IsDBNull(v) Then Return """" & name & """:null"
        Return """" & name & """:""" & CStr(v).Replace("\", "\\").Replace("""", "\""") & """"
    End Function

    Private Function JNum(ByVal name As String, ByVal v As Decimal) As String
        Return """" & name & """:" & v.ToString("0.00", m_Inv)
    End Function

    Private Function GetStr(ByVal d As Dictionary(Of String, Object), ByVal k As String) As String
        If d Is Nothing OrElse Not d.ContainsKey(k) OrElse d(k) Is Nothing Then Return ""
        Return CStr(d(k)).Trim()
    End Function

    Private Function GetDec(ByVal d As Dictionary(Of String, Object), ByVal k As String) As Decimal
        If d Is Nothing OrElse Not d.ContainsKey(k) OrElse d(k) Is Nothing Then Return 0D
        Return Convert.ToDecimal(d(k), m_Inv)
    End Function

End Class
