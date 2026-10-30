// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Synthetic second C# unit for the structure-tool fixture. Not customer code.
// It repeats the approval-threshold check and the proc's own error message, so the duplicate-logic
// and shared-message detectors in facts_post.py have something real to find across files.

using System;
using System.Data;
using System.Data.SqlClient;

namespace Contoso.Ap
{
    public class ApPaymentService
    {
        // Business rule: the payment side applies the SAME approval threshold as posting.
        private const decimal ApprovalThreshold = 5000m;

        private readonly ApInvoiceRepository _repo;

        public ApPaymentService(ApInvoiceRepository repo)
        {
            _repo = repo;
        }

        public string Validate(byte apCo, int apTrans)
        {
            DataRow header = _repo.LoadHeader(apCo, apTrans);
            decimal grossAmt = Convert.ToDecimal(header["GrossAmt"]);
            if (grossAmt >= ApprovalThreshold)
            {
                return "Invoice over the approval limit requires approval before posting.";
            }
            return null;
        }

        /// <summary>A hard-coded second copy of the approval threshold.</summary>
        public bool NeedsApproval(decimal grossAmt)
        {
            // The literal is the drift: usp_ApInvPost uses a STRICTER boundary (> 5000) for the
            // same number, so an invoice of exactly 5000 is treated differently by the two layers.
            return grossAmt >= 5000m;
        }

        public DataTable OpenPayables(byte apCo)
        {
            string sql = "SELECT ApCo, ApTrans, Vendor, GrossAmt FROM dbo.ApInvHdr "
                       + "WHERE ApCo = @ApCo AND Status = 'P'";
            var table = new DataTable();
            using (var cn = new SqlConnection("placeholder"))
            using (var cmd = new SqlCommand(sql, cn))
            {
                cmd.Parameters.AddWithValue("@ApCo", apCo);
                new SqlDataAdapter(cmd).Fill(table);
            }
            return table;
        }
    }
}
