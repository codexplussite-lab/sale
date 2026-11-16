# ... existing code ...
@app.get("/api/leads")
def list_leads(
    status: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    min_score: int = Query(default=0),
    region: Optional[str] = Query(default=None),
    has_budget: bool = Query(default=False),
    q: Optional[str] = Query(default=None),
):
    clauses = ["intent_score >= %s"]
    params: list[object] = [min_score]

    if status and status != "all":
        clauses.append("status = %s")
        params.append(status)
    if platform and platform != "all":
        clauses.append("platform = %s")
        params.append(platform)
    if region == "usa":
        clauses.append(
            "(raw_text ~* '\\\\m(USA|US|United States|USD|EST|PST|CST|MST|New York|California|Texas|Florida)\\\\M' OR raw_text ILIKE '%%$%%')"
        )
    elif region == "uk_eu":
        clauses.append(
            "(raw_text ~* '\\\\m(UK|United Kingdom|London|Europe|EU|Germany|France|GBP|EUR)\\\\M' OR raw_text ILIKE '%%£%%' OR raw_text ILIKE '%%€%%')"
        )
    if has_budget:
        clauses.append(
            "(raw_text ~* '(\\$|USD|EUR|GBP|£|€|budget|paying|rate|/hr|fixed price)' OR pain_point_summary ~* '(\\$|USD|budget)')"
        )
    if q:
        clauses.append("(raw_text ILIKE %s OR pain_point_summary ILIKE %s OR author_handle ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])

    where_sql = " AND ".join(clauses)
# ... existing code ...
@app.post("/api/leads/{lead_id}/regenerate")
def regenerate_lead_reply(lead_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, author_handle, raw_text FROM social_leads WHERE id = %s;",
                (lead_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Lead not found")

        lead = {"platform": row[0], "author_handle": row[1] or "", "raw_text": row[2] or ""}
        try:
            scored = score_with_gemini(lead)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini error: {exc}")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE social_leads
                SET drafted_reply = %s, pain_point_summary = %s, updated_at = NOW()
                WHERE id = %s;
                """,
                (scored["drafted_reply"], scored["pain_point_summary"], lead_id),
            )
        conn.commit()

    return {
        "id": lead_id,
        "drafted_reply": scored["drafted_reply"],
        "pain_point_summary": scored["pain_point_summary"],
    }


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadUpdate):
# ... existing code ...
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE social_leads SET {', '.join(updates)} WHERE id = %s RETURNING id;",
                params,
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Lead not found")
        conn.commit()
    return {"updated": True, "id": lead_id}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_leads WHERE id = %s RETURNING id;", (lead_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Lead not found")
        conn.commit()
    return {"deleted": True, "id": lead_id}


@app.delete("/api/leads")
def bulk_delete_leads(status: Optional[str] = Query(default=None)):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if status and status != "all":
                cur.execute("DELETE FROM social_leads WHERE status = %s;", (status,))
            else:
                cur.execute("DELETE FROM social_leads;")
            deleted_count = cur.rowcount
        conn.commit()
    return {"deleted": deleted_count}


@app.post("/api/sync")
# ... existing code ...