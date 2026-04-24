package com.example;

public class Sample {
    public String render() {
        String sql = """
            SELECT *
            FROM users
            WHERE id = ?
            """;
        return sql;
    }
}
