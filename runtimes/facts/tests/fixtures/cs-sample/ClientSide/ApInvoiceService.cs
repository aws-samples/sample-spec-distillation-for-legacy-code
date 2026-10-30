// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Synthetic C# AP invoice service for the structure-tool fixture. Not customer code.
// Exercises exactly what the C# rig has to find: a class, a method calling another method, a
// SqlCommand carrying a stored-procedure name, a const threshold used in a comparison, inline SQL
// naming tables, a DB column referenced through a row indexer, and a rule stated only in a comment.

using System;
using System.Data;
using System.Data.SqlClient;

namespace Contoso.Ap
{
    public class ApInvoiceService
    {
        // Business rule: invoices at or above this gross amount route to approval before posting.
        // The same threshold is enforced again in usp_ApInvPost, so the two must stay in step.
        private const decimal ApprovalThreshold = 5000m;
        private const string PostProcName = "usp_ApInvPost";

        private readonly ApInvoiceRepository _repo;

        public ApInvoiceService(ApInvoiceRepository repo)
        {
            _repo = repo;
        }

        /// <summary>Posts an invoice, routing it to approval when it is over the threshold.</summary>
        public int PostInvoice(byte apCo, int apTrans)
        {
            DataRow header = _repo.LoadHeader(apCo, apTrans);
            decimal grossAmt = Convert.ToDecimal(header["GrossAmt"]);

            // Business rule: an invoice over the approval threshold must not post directly.
            // usp_ApInvPost enforces the same threshold with a STRICTER boundary (> 5000), which is
            // the cross-layer inconsistency this fixture exists to surface.
            if (grossAmt >= ApprovalThreshold)
            {
                RouteToApproval(apCo, apTrans);
                return 1;
            }

            return _repo.ExecutePost(apCo, apTrans);
        }

        private void RouteToApproval(byte apCo, int apTrans)
        {
            _repo.SetStatus(apCo, apTrans, "A");
        }

        public decimal LineTotal(byte apCo, int apTrans)
        {
            DataTable lines = _repo.LoadLines(apCo, apTrans);
            decimal total = 0m;
            foreach (DataRow r in lines.Rows)
            {
                total += Convert.ToDecimal(r["Amount"]);
            }
            return total;
        }
    }

    public class ApInvoiceRepository
    {
        private readonly string _connectionString;

        public ApInvoiceRepository(string connectionString)
        {
            _connectionString = connectionString;
        }

        public DataRow LoadHeader(byte apCo, int apTrans)
        {
            string sql = "SELECT ApCo, ApTrans, Vendor, ApRef, GrossAmt, Status FROM dbo.ApInvHdr "
                       + "WHERE ApCo = @ApCo AND ApTrans = @ApTrans";
            var table = new DataTable();
            using (var cn = new SqlConnection(_connectionString))
            using (var cmd = new SqlCommand(sql, cn))
            {
                cmd.Parameters.AddWithValue("@ApCo", apCo);
                cmd.Parameters.AddWithValue("@ApTrans", apTrans);
                new SqlDataAdapter(cmd).Fill(table);
            }
            return table.Rows[0];
        }

        public DataTable LoadLines(byte apCo, int apTrans)
        {
            string sql = "SELECT LineNum, Amount FROM dbo.ApInvLine WHERE ApCo = @ApCo AND ApTrans = @ApTrans";
            var table = new DataTable();
            using (var cn = new SqlConnection(_connectionString))
            using (var cmd = new SqlCommand(sql, cn))
            {
                new SqlDataAdapter(cmd).Fill(table);
            }
            return table;
        }

        public int ExecutePost(byte apCo, int apTrans)
        {
            using (var cn = new SqlConnection(_connectionString))
            using (var cmd = new SqlCommand("usp_ApInvPost", cn))
            {
                cmd.CommandType = CommandType.StoredProcedure;
                cmd.Parameters.AddWithValue("@ApCo", apCo);
                cmd.Parameters.AddWithValue("@ApTrans", apTrans);
                cn.Open();
                return cmd.ExecuteNonQuery();
            }
        }

        public void SetStatus(byte apCo, int apTrans, string status)
        {
            string sql = "UPDATE dbo.ApInvHdr SET Status = @Status WHERE ApCo = @ApCo AND ApTrans = @ApTrans";
            using (var cn = new SqlConnection(_connectionString))
            using (var cmd = new SqlCommand(sql, cn))
            {
                cmd.Parameters.AddWithValue("@Status", status);
                cn.Open();
                cmd.ExecuteNonQuery();
            }
        }
    }
}
