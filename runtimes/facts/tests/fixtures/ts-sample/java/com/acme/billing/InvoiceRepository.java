// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
package com.acme.billing;

import java.sql.Connection;

public class InvoiceRepository {
    private final Connection conn;

    public InvoiceRepository(Connection conn) {
        this.conn = conn;
    }

    public void markPaid(long id) throws Exception {
        conn.prepareStatement("UPDATE invoices SET status = 'PAID' WHERE id = ?").execute();
    }
}
