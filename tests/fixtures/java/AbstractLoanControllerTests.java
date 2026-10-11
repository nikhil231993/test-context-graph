package org.example.library.web;

import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import org.example.library.model.Loan;
import org.example.library.service.LoanService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/**
 * Shared tests for {@link LoanController}.
 */
abstract class AbstractLoanControllerTests {

	@Autowired
	protected LoanController loanController;

    @MockitoBean
    private LoanService loanService;

    protected MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        this.mockMvc = MockMvcBuilders.standaloneSetup(loanController).build();
    }

    @Test
    void shouldGetLoan() throws Exception {
        given(this.loanService.findLoan(1)).willReturn(new Loan());
        this.mockMvc.perform(get("/api/loans/1")
                .accept(MediaType.APPLICATION_JSON))
            .andExpect(status().isOk());
    }

    @Test
    void shouldPageLoans() throws Exception {
	    mockMvc.perform(get("/api/v2/loans?page=0&size=2"))
	        .andExpect(status().isOk());
    }

    @Test
    void shouldRenewLoan() throws Exception {
        int memberId = 7;
        String loanId = "12";
        mockMvc.perform(put("/api/members/" + memberId + "/loans/" + loanId + "/renewal")
                .contentType(MediaType.APPLICATION_JSON))
            .andExpect(status().isNoContent());
    }
}
