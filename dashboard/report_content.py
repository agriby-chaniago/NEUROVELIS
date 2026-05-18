"""
dashboard/report_content.py

Content data for report generation: display labels, enriched summaries,
and condition-specific recommendations. Separated from report_pdf.py to
keep render logic and content data independently maintainable.
"""

DISPLAY_LABELS = {
    "stress":     "Indikasi Stres",
    "anxiety":    "Indikasi Kecemasan",
    "depression": "Indikasi Gejala Depresif",
    "normal":     "Kondisi Relatif Stabil",
}

# (threshold_pct, text) — first bucket where confidence% <= threshold is used
SUMMARY_BUCKETS = {
    "stress": [
        (25,
         "Terlihat sedikit kecenderungan respons stres selama pemeriksaan. "
         "Perubahan pada denyut jantung dan konduktansi kulit (GSR) masih dalam batas wajar "
         "dan kemungkinan bersifat situasional — dapat dipengaruhi kondisi sementara seperti "
         "aktivitas fisik ringan atau kegelisahan sesaat. "
         "Tidak terdapat pola yang mengindikasikan tekanan psikologis yang berarti."),
        (50,
         "Hasil biometrik menunjukkan kecenderungan respons stres ringan hingga sedang. "
         "Pola denyut jantung dan aktivitas GSR dapat berkaitan dengan aktivasi sistem saraf "
         "simpatik yang lebih tinggi dari biasanya, kemungkinan mencerminkan tekanan dari "
         "beban kerja, tuntutan sosial, atau kondisi lingkungan saat ini. "
         "Pengelolaan stres secara aktif sangat dianjurkan."),
        (75,
         "Pola biometrik menunjukkan respons stres yang cukup konsisten selama pemeriksaan. "
         "Deviasi pada parameter denyut jantung dan GSR kemungkinan mencerminkan sistem saraf "
         "simpatik yang aktif secara berkelanjutan, yang dapat berkaitan dengan tekanan "
         "psikologis yang berlangsung dalam waktu tertentu atau akumulasi stresor yang perlu "
         "ditangani. Langkah aktif untuk mengurangi beban stres sangat direkomendasikan."),
        (100,
         "Respons biometrik menunjukkan kecenderungan tingkat stres yang dominan dan menetap "
         "sepanjang pemeriksaan. Pola HR, GSR, dan SpO2 secara konsisten kemungkinan "
         "mencerminkan kondisi hiperaktivasi fisiologis yang perlu mendapat perhatian. "
         "Perlu dipertimbangkan untuk berkonsultasi dengan profesional kesehatan mental "
         "guna evaluasi lebih lanjut."),
    ],
    "anxiety": [
        (25,
         "Terlihat sedikit peningkatan respons fisiologis selama pemeriksaan, namun masih "
         "dalam tingkat yang ringan dan belum mengindikasikan gangguan kecemasan yang bermakna. "
         "Fluktuasi kecil pada denyut jantung dan konduktansi kulit kemungkinan bersifat "
         "reaktif terhadap situasi pemeriksaan itu sendiri."),
        (50,
         "Pola biometrik menunjukkan kecenderungan ketegangan atau kecemasan ringan. "
         "Peningkatan aktivitas denyut jantung dan GSR dapat berkaitan dengan sistem saraf "
         "otonom dalam kondisi waspada yang lebih tinggi dari biasanya, yang kemungkinan "
         "mencerminkan kekhawatiran situasional atau kegelisahan yang perlu diperhatikan."),
        (75,
         "Respons biometrik menunjukkan pola kecemasan yang cukup konsisten selama pemeriksaan. "
         "Parameter fisiologis kemungkinan mencerminkan aktivasi respons \"fight-or-flight\" "
         "yang persisten, yang dapat berkaitan dengan kecemasan yang menjadi pola berulang "
         "dan perlu mendapat perhatian lebih. Konsultasi dengan profesional sangat dianjurkan."),
        (100,
         "Pola biometrik menunjukkan kecenderungan tingkat kecemasan yang dominan dengan "
         "respons fisiologis yang intens dan konsisten. Deviasi parameter vital yang signifikan "
         "kemungkinan berkaitan dengan kecemasan berat yang dapat memengaruhi fungsi keseharian. "
         "Perlu dipertimbangkan evaluasi dengan tenaga profesional kesehatan mental "
         "sesegera mungkin."),
    ],
    "depression": [
        (25,
         "Aktivitas biometrik terlihat relatif tenang selama pemeriksaan dengan denyut jantung "
         "dan konduktansi kulit di bawah rata-rata. Pola ini masih dapat dipengaruhi berbagai "
         "faktor sementara seperti kelelahan fisik, kondisi istirahat, atau variasi normal "
         "individu dan belum mengindikasikan kondisi yang bermakna secara klinis."),
        (50,
         "Pola biometrik menunjukkan kecenderungan aktivitas fisiologis yang lebih rendah dari "
         "biasanya, dengan denyut jantung dan respons GSR yang cenderung lebih lamban. "
         "Kondisi ini dapat berkaitan dengan kelelahan emosional, penurunan motivasi, atau "
         "berkurangnya energi yang kemungkinan perlu mendapat perhatian."),
        (75,
         "Hasil biometrik menunjukkan pola aktivitas fisiologis rendah yang cukup konsisten. "
         "Parameter vital yang secara sistematis berada di bawah baseline kemungkinan "
         "mencerminkan kondisi yang dapat berkaitan dengan gejala seperti kelelahan persisten "
         "atau penurunan energi. Konsultasi dengan psikolog atau dokter sangat direkomendasikan "
         "untuk evaluasi lebih lanjut."),
        (100,
         "Pola biometrik menunjukkan kecenderungan aktivitas fisiologis rendah yang dominan "
         "dan konsisten sepanjang pemeriksaan. Parameter vital yang secara signifikan berada "
         "di bawah rentang yang diharapkan kemungkinan mencerminkan kondisi yang perlu mendapat "
         "evaluasi klinis profesional. Diperlukan konsultasi dengan tenaga kesehatan mental "
         "sesegera mungkin."),
    ],
    "normal": [
        (100,
         "Parameter fisiologis berada dalam rentang yang sehat dan diharapkan selama seluruh "
         "durasi pemeriksaan. Denyut jantung, saturasi oksigen, dan konduktansi kulit "
         "menunjukkan keseimbangan sistem saraf otonom yang baik, kemungkinan mencerminkan "
         "kondisi psikofisiologis yang relatif stabil. Pertahankan pola hidup sehat, "
         "manajemen stres yang baik, dan istirahat yang cukup untuk menjaga kondisi ini."),
    ],
}

# (threshold_pct, list_of_groups) — same bucket logic as SUMMARY_BUCKETS
RECOMMENDATION_BUCKETS = {
    "stress": [
        (25, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Praktikkan latihan napas dalam (teknik 4-7-8) selama 5 menit sebelum tidur",
                "Luangkan 15 menit setiap hari untuk aktivitas yang Anda nikmati",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Terapkan teknik Pomodoro: 25 menit fokus, 5 menit istirahat",
                "Buat daftar prioritas harian untuk mengurangi beban mental",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Jaga jadwal tidur yang konsisten (7–8 jam)",
                "Lakukan olahraga ringan 3× seminggu seperti jalan kaki 30 menit",
            ]},
        ]),
        (50, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Coba meditasi kesadaran penuh (mindfulness) 10–15 menit per hari",
                "Praktikkan relaksasi otot progresif (progressive muscle relaxation) sebelum tidur",
                "Kurangi konsumsi kafein, terutama setelah pukul 14.00",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Batasi jam kerja maksimal 8 jam dan hindari membawa pekerjaan ke rumah",
                "Ambil istirahat aktif setiap 90 menit — berdiri, berjalan, atau lakukan peregangan",
                "Delegasikan tugas yang dapat dikerjakan orang lain",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Prioritaskan tidur 7–9 jam dengan jadwal yang konsisten",
                "Kurangi paparan berita atau media sosial yang memicu kegelisahan",
                "Habiskan waktu di luar ruangan minimal 20 menit setiap hari",
            ]},
        ]),
        (75, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Pertimbangkan sesi yoga atau tai chi minimal 2× seminggu",
                "Coba journaling: tulis 3 hal yang Anda syukuri dan pemicu stres setiap malam",
                "Gunakan teknik grounding (kesadaran indrawi) saat merasa kewalahan",
                "Pijat atau fisioterapi dapat membantu melepaskan ketegangan fisik",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Evaluasi ulang beban kerja dan diskusikan dengan atasan jika terlalu berat",
                "Tetapkan batas waktu yang jelas antara pekerjaan dan waktu pribadi",
                "Fokus pada 1–3 tugas penting per hari dan delegasikan sisanya",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Pertimbangkan konsultasi dengan psikolog atau konselor untuk pendampingan lebih lanjut",
                "Bangun jaringan dukungan sosial — ceritakan perasaan kepada orang terpercaya",
                "Hindari alkohol dan rokok sebagai pelarian karena dapat memperburuk stres jangka panjang",
            ]},
        ]),
        (100, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Jadwalkan konsultasi dengan psikolog atau psikiater untuk pendampingan profesional",
                "Pertimbangkan terapi kognitif-perilaku (CBT) yang terbukti efektif untuk stres kronis",
                "Latihan pernapasan kotak (box breathing): tarik 4 hitungan, tahan 4, buang 4, tahan 4",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Pertimbangkan cuti atau pengurangan beban kerja sementara jika memungkinkan",
                "Komunikasikan kondisi Anda kepada atasan atau bagian SDM — banyak institusi memiliki program dukungan karyawan",
                "Hindari mengambil tugas baru hingga kondisi membaik",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Prioritaskan kebutuhan dasar: tidur cukup, makan teratur, dan cairan yang memadai",
                "Beri diri Anda izin untuk beristirahat tanpa rasa bersalah",
                "Jauhi situasi atau kondisi yang secara konsisten memperburuk keadaan Anda",
            ]},
        ]),
    ],
    "anxiety": [
        (25, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Praktikkan napas diafragma: tarik napas dari perut selama 4 detik, buang perlahan 6 detik",
                "Dengarkan musik menenangkan atau suara alam saat beristirahat",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Rencanakan hari kerja dari malam sebelumnya untuk mengurangi ketidakpastian",
                "Batasi waktu memeriksa pesan dan email agar tidak terus-menerus terganggu",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Kurangi konsumsi kafein dan gula berlebih yang dapat memperburuk kegelisahan",
                "Jaga rutinitas harian yang terstruktur — ketidakpastian dapat memperburuk kecemasan",
            ]},
        ]),
        (50, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Coba teknik kesadaran penuh (mindfulness): fokus penuh pada aktivitas saat ini",
                "Buat catatan kekhawatiran — tulis semua yang menggelisahkan, lalu tentukan mana yang dapat dikontrol",
                "Praktikkan relaksasi otot progresif sebelum tidur",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Buat checklist harian yang realistis untuk mengurangi rasa kewalahan",
                "Hindari multitasking — fokus pada satu tugas dalam satu waktu",
                "Komunikasikan ekspektasi atau tenggat yang tidak realistis kepada tim atau atasan",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Batasi penggunaan media sosial terutama di pagi dan malam hari",
                "Habiskan waktu di alam terbuka — terbukti membantu mengurangi gejala kecemasan",
                "Bangun rutinitas malam yang menenangkan dan hindari layar 1 jam sebelum tidur",
            ]},
        ]),
        (75, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Pertimbangkan terapi dengan psikolog, khususnya CBT atau terapi berbasis kesadaran penuh",
                "Latihan pernapasan 4-7-8: tarik napas 4 detik, tahan 7 detik, buang perlahan 8 detik",
                "Journaling mendalam tentang pemicu kecemasan dapat membantu mengidentifikasi pola",
                "Yoga restoratif atau meditasi terpandu sangat direkomendasikan",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Terapkan time-boxing: alokasikan waktu spesifik per tugas dan patuhi batasnya",
                "Identifikasi dan kurangi paparan terhadap pemicu kecemasan di lingkungan kerja",
                "Jangan ragu meminta bantuan rekan — berbagi beban dapat mengurangi tekanan",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Olahraga aerobik teratur (jogging, renang, bersepeda) terbukti secara ilmiah mengurangi kecemasan",
                "Evaluasi kualitas tidur — kecemasan dan gangguan tidur saling memengaruhi",
                "Pertimbangkan bergabung dengan kelompok dukungan atau komunitas kesehatan mental",
            ]},
        ]),
        (100, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Segera konsultasikan kondisi Anda dengan psikiater atau psikolog klinis untuk pendampingan profesional",
                "Saat merasa sangat cemas, gunakan teknik grounding: pegang objek fisik dan fokus pada teksturnya",
                "Jangan hadapi kondisi ini sendirian — cari dukungan profesional atau orang terdekat",
                "Jika membutuhkan bantuan segera, hubungi Into The Light Indonesia: 119 ext 8",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Pertimbangkan pengurangan beban kerja atau cuti jika kecemasan mengganggu produktivitas",
                "Bicarakan kondisi dengan bagian SDM atau layanan kesehatan karyawan jika tersedia",
                "Hindari situasi yang secara konsisten memicu kecemasan berat hingga mendapat penanganan",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Jalin komunikasi dengan orang terdekat — isolasi dapat memperburuk kondisi kecemasan",
                "Batasi konsumsi alkohol karena dapat memperburuk kecemasan dalam jangka panjang",
                "Patuhi semua rekomendasi dari profesional kesehatan mental yang Anda temui",
            ]},
        ]),
    ],
    "depression": [
        (25, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Habiskan waktu di bawah sinar matahari pagi minimal 15–20 menit",
                "Coba aktivitas kreatif ringan seperti menulis, melukis, atau berkebun",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Tetapkan target kecil yang mudah dicapai setiap hari untuk membangun rasa pencapaian",
                "Pastikan ruang kerja mendapat cukup cahaya alami",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Jaga jadwal makan dan tidur yang teratur — ritme sirkadian yang stabil mendukung suasana hati",
                "Habiskan waktu bersama orang-orang yang memberi energi positif",
            ]},
        ]),
        (50, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Mulai dengan olahraga ringan walau hanya 10–20 menit — aktivitas fisik membantu memperbaiki suasana hati",
                "Coba aktivasi perilaku (behavioral activation): lakukan aktivitas yang dulu Anda nikmati meski belum terasa menyenangkan",
                "Paparan sinar matahari pagi sangat penting untuk regulasi suasana hati — jalan pagi sangat dianjurkan",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Hindari isolasi di tempat kerja — cobalah makan siang bersama rekan",
                "Rayakan pencapaian kecil setiap hari untuk membangun momentum positif",
                "Mulai dari tugas paling mudah jika motivasi terasa sangat rendah",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Perhatikan pola makan — kekurangan vitamin D, omega-3, atau zat besi dapat memengaruhi suasana hati",
                "Batasi konsumsi alkohol yang bersifat menekan sistem saraf",
                "Pertimbangkan konsultasi awal dengan psikolog untuk penilaian lebih lanjut",
            ]},
        ]),
        (75, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Konsultasikan kondisi Anda dengan psikolog untuk kemungkinan terapi seperti CBT atau terapi interpersonal (IPT)",
                "Bangun jadwal aktivitas harian yang terstruktur meski terasa berat untuk dilakukan",
                "Jika membutuhkan dukungan segera, hubungi Into The Light Indonesia: 119 ext 8",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Evaluasi apakah beban kerja berkontribusi pada kondisi saat ini dan pertimbangkan penyesuaian",
                "Beritahukan kondisi kepada seseorang yang dipercaya di tempat kerja",
                "Tetapkan ekspektasi yang realistis dan kurangi perfeksionisme yang dapat memperburuk kondisi",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Olahraga teratur sangat direkomendasikan — mulai bertahap dari yang ringan",
                "Jaga koneksi sosial meski terasa sulit — isolasi dapat memperparah kondisi secara signifikan",
                "Jadwal tidur yang teratur adalah prioritas — hindari tidur berlebihan di siang hari",
            ]},
        ]),
        (100, [
            {"kategori": "Relaksasi & Pemulihan", "items": [
                "Segera hubungi psikiater atau psikolog klinis untuk evaluasi dan pendampingan profesional",
                "Jika memiliki pikiran menyakiti diri, segera hubungi Into The Light Indonesia: 119 ext 8",
                "Kombinasi terapi dan medikasi (jika direkomendasikan dokter) terbukti efektif untuk kondisi berat",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Pertimbangkan cuti medis jika kondisi mengganggu kemampuan bekerja",
                "Libatkan orang terdekat atau bagian SDM dalam mendapatkan bantuan yang diperlukan",
                "Fokuskan energi pada pemulihan sebagai prioritas utama saat ini",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Bangun sistem dukungan sosial yang kuat — keluarga, teman, atau kelompok dukungan",
                "Patuhi rencana pengobatan dan jadwal terapi yang direkomendasikan profesional",
                "Rawat kebutuhan dasar: tidur, makan, dan kebersihan diri meski terasa sangat berat",
            ]},
        ]),
    ],
    "normal": [
        (100, [
            {"kategori": "Menjaga Kebugaran", "items": [
                "Pertahankan rutinitas olahraga minimal 150 menit per minggu (rekomendasi WHO)",
                "Lanjutkan praktik relaksasi atau kesadaran penuh (mindfulness) rutin yang sudah berjalan",
            ]},
            {"kategori": "Pola Kerja", "items": [
                "Terapkan keseimbangan kerja-kehidupan (work-life balance) yang sehat",
                "Kelola stres secara proaktif sebelum menumpuk",
            ]},
            {"kategori": "Pola Hidup", "items": [
                "Jaga pola tidur 7–8 jam yang konsisten sebagai fondasi kesehatan mental",
                "Investasikan waktu dalam hubungan sosial yang positif dan bermakna",
                "Lakukan pemeriksaan kesehatan mental secara berkala seperti yang telah Anda lakukan hari ini",
            ]},
        ]),
    ],
}


def get_summary(condition: str, confidence: float) -> str:
    buckets = SUMMARY_BUCKETS.get(condition, SUMMARY_BUCKETS["normal"])
    pct = max(0.0, min(confidence * 100, 100.0))
    for threshold, text in buckets:
        if pct <= threshold:
            return text
    return buckets[-1][1]


def get_recommendation(condition: str, confidence: float) -> list:
    buckets = RECOMMENDATION_BUCKETS.get(condition, RECOMMENDATION_BUCKETS["normal"])
    pct = max(0.0, min(confidence * 100, 100.0))
    for threshold, recs in buckets:
        if pct <= threshold:
            return recs
    return buckets[-1][1]


def get_display_label(condition: str) -> str:
    return DISPLAY_LABELS.get(condition, condition.title())


def get_severity_label(confidence: float) -> str:
    pct = max(0.0, min(confidence * 100, 100.0))
    if pct <= 25:
        return "Rendah"
    elif pct <= 50:
        return "Ringan"
    elif pct <= 75:
        return "Sedang"
    else:
        return "Tinggi"
