# ... (앞부분 설정 및 인증 코드는 동일하게 유지) ...

def run_harvester():
    start_time = time.time()
    success_count = 0
    error_count = 0
    
    try:
        # 경로 및 모드 설정 로직 (기존과 동일)
        root_id = find_file_id("US_Alpha_Seeker")
        sys_id = find_file_id("System_Identity_Maps", root_id)
        data_id = find_file_id("Financial_Data_5Y_Split", sys_id)
        
        now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        update_mode = "DAILY" 
        if now_kst.weekday() == 5: update_mode = "WEEKLY"
        if now_kst.day in [1, 15]: update_mode = "QUARTERLY"

        # 그룹 판별 및 필터링
        current_hour = now_kst.hour
        target_chars = "ABCDEFGHIJKLM" if 6 <= current_hour <= 8 else "NOPQRSTUVWXYZ0123456789"
        
        mapping_id = find_file_id("Ticker_ID_Mapping_Final.json", sys_id)
        full_map = download_json(mapping_id) if mapping_id else {}
        
        filtered_map = {t: i for t, i in full_map.items() if (t[0].upper() in target_chars) or (not t[0].isalpha() and "0123456789" in target_chars)}

        send_telegram(f"🚀 *수집 프로세스 시작*\n- 그룹: {target_chars}\n- 모드: {update_mode}\n- 대상: {len(filtered_map)} 종목")

        storage = {}
        session = requests.Session()

        # 데이터 수집 루프
        for ticker, msn_id in filtered_map.items():
            first_char = ticker[0].upper()
            filename = f"{first_char if first_char.isalpha() else 'ETC'}_stocks.json"
            
            try:
                if filename not in storage:
                    fid = find_file_id(filename, data_id)
                    storage[filename] = download_json(fid) if fid else {}

                # MSN API 호출 (생략: 기존 로직과 동일)
                # ... 수집 코드 ...
                success_count += 1
                time.sleep(random.uniform(0.7, 1.0)) # 밴 방지를 위해 약간 더 여유있게 조정

                # [개선] 500종목마다 로그 출력하여 GitHub 생존 신고
                if success_count % 500 == 0:
                    print(f"🔄 현재 {success_count}개 수집 중...")

            except Exception:
                error_count += 1

        # --- [저장 로직 개선] ---
        send_telegram(f"📤 *저장 단계 진입*: {success_count}개 데이터를 드라이브에 기록합니다.")
        
        for fname, content in storage.items():
            try:
                upload_json(fname, content, data_id)
                # 파일별 저장 성공 알림 (중간에 끊겨도 어디까지 됐는지 알 수 있음)
                send_telegram(f"✅ 파일 저장 완료: `{fname}`")
                time.sleep(2) # 구글 API 과부하 방지
            except Exception as e:
                send_telegram(f"⚠️ `{fname}` 저장 중 오류 발생: {str(e)}")

        # 최종 보고
        duration = (time.time() - start_time) / 60
        summary = (
            f"✨ *최종 수집 완료 보고*\n"
            f"━━━━━━━━━━━━━━\n"
            f"✅ 성공: {success_count}\n"
            f"❌ 에러: {error_count}\n"
            f"⏱️ 소요: {duration:.1f}분\n"
            f"━━━━━━━━━━━━━━"
        )
        send_telegram(summary)

    except Exception as e:
        send_telegram(f"🚨 *시스템 중단 에러*: {str(e)}")

if __name__ == "__main__":
    run_harvester()
