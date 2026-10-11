package org.example.library.repository.jdbc;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;

import org.example.library.model.Book;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

@Repository
public class JdbcBookRepository {

	private final JdbcTemplate jdbcTemplate;

	public JdbcBookRepository(JdbcTemplate jdbcTemplate) {
		this.jdbcTemplate = jdbcTemplate;
	}

	public List<Book> findByAuthor(int authorId) {
		return this.jdbcTemplate.query(
				"SELECT id, title FROM books WHERE author_id = ?",
				new RowMapper<Book>() {
					@Override
					public Book mapRow(ResultSet rs, int rowNum) throws SQLException {
						Book book = new Book();
						book.setTitle(rs.getString("title"));
						return book;
					}
				},
				authorId);
	}
}
