"""
core/ews_signal.py
===================
緊急警報放送（EWS）信号音（AFSK方式）を、外部ファイルへ出力せずメモリ上で
生成するモジュール。昭和60年郵政省告示第405号に準拠したビット列組み立て・
PCM波形合成のロジックを提供する。

【由来】
スタンドアロンのGUI/CLIツール（ews_generator.py）から、
ビット列生成（generate_ews_bitstring）とPCM波形合成（synthesize_ews_audio）の
コアロジックのみを移植した。元ツールにあった以下の要素は、Bot組み込み用途
では不要なため含めていない:
  - Tkinter GUI（画面のないRaspberry Pi運用が前提のため）
  - argparse による CLI 実行モード
  - WAVファイルへの保存（save_wav_file）・os.system 経由の再生
    （save_wav_file はテスト・デバッグ用の generate_ews_wav_bytes_for_debug
    としてのみ残し、Bot本体の通知経路では使用しない。実際の再生は
    core/audio.py 側で pygame.mixer.Sound(buffer=...) を使い、
    ファイルに書き出さずメモリ上のPCMをそのまま再生する）

【使い方】
    from core.ews_signal import generate_ews_pcm

    pcm_bytes = generate_ews_pcm(
        region_key="熊本県",
        blocks=6,
        pre_tone_sec=0.2,
        post_tone_sec=0.2,
    )
    # pcm_bytes は 16bit / モノラル / 44100Hz の生PCMバイト列。
    # core.audio.AudioMixin.play_ews_pcm() 等でそのまま再生する。

【信号種別について】
今回のBot組み込みでは「第二種開始信号」（津波警報等用）のみを使用する。
第一種開始信号（大規模地震・国民保護等）・終了信号は、今回のユースケース
（津波警報・大津波警報の発表・更新通知）では使用しないため未実装。
将来的に必要になった場合は generate_ews_bitstring の sig_type 引数を
拡張する形で対応できる（元ロジックの構造はそのまま踏襲している）。
"""
import math
import struct
import datetime

# ==============================================================================
# 警報音・AFSK制御用定数設定 (昭和60年郵政省告示第405号 準拠)
# ==============================================================================
SAMPLE_RATE = 44100      # 音声サンプリング周波数 (Hz)
FREQ_MARK = 1024         # 論理1 (マーク): 1024 Hz
FREQ_SPACE = 640         # 論理0 (スペース): 640 Hz
BIT_RATE = 64            # 伝送速度: 64 bps
SAMPLES_PER_BIT = int(SAMPLE_RATE / BIT_RATE)  # 1ビットあたりのサンプル数 (689)

# ==============================================================================
# 別表第1号 地域符号 (12ビット)
# ==============================================================================
REGION_CODES = {
    # 全国共通・広域圏
    "全国共通": "001101001101",
    "関東広域圏": "010110100101",
    "中京広域圏": "011100101010",
    "近畿広域圏": "100011010101",
    "鳥取・島根圏": "011010011001",
    "岡山・香川圏": "010101010011",
    # 47都道府県
    "北海道": "000101101011", "青森県": "010001100111", "岩手県": "010111010100",
    "宮城県": "011101011000", "秋田県": "101011000110", "山形県": "111001001100",
    "福島県": "000110101110", "茨城県": "110001101001", "栃木県": "111000111000",
    "群馬県": "100110001011", "埼玉県": "011001001011", "千葉県": "000111000111",
    "東京都": "101010101100", "神奈川県": "010101101100", "新潟県": "010011001110",
    "富山県": "010100111001", "石川県": "011010100110", "福井県": "100100101101",
    "山梨県": "110101001010", "長野県": "100111010010", "岐阜県": "101001100101",
    "静岡県": "101001011010", "愛知県": "100101100110", "三重県": "001011011100",
    "滋賀県": "110011100100", "京都府": "010110011010", "大阪府": "110010110010",
    "兵庫県": "011001110100", "奈良県": "101010010011", "和歌山県": "001110010110",
    "鳥取県": "110100100011", "島根県": "001100011011", "岡山県": "001010110101",
    "広島県": "101100110001", "山口県": "101110011000", "徳島県": "111001100010",
    "香川県": "100110110100", "愛媛県": "000110011101", "高知県": "001011100011",
    "福岡県": "011000101101", "佐賀県": "100101011001", "長崎県": "101000101011",
    "熊本県": "100010100111", "大分県": "110010001101", "宮崎県": "110100011100",
    "鹿児島県": "110101000101", "沖縄県": "001101110010",
}

# ==============================================================================
# 日時変換テーブル (別表第2号〜第5号 5ビット構造)
# ==============================================================================
DAY_CODES = {
    1: "10000", 2: "01000", 3: "11000", 4: "00100", 5: "10100",
    6: "01100", 7: "11100", 8: "00010", 9: "10010", 10: "01010",
    11: "11010", 12: "00110", 13: "10110", 14: "01110", 15: "11110",
    16: "00001", 17: "10001", 18: "01001", 19: "11001", 20: "00101",
    21: "10101", 22: "01101", 23: "11101", 24: "00011", 25: "10011",
    26: "01011", 27: "11011", 28: "00111", 29: "10111", 30: "01111", 31: "11111",
}

MONTH_CODES = {
    1: "10001", 2: "01001", 3: "11001", 4: "00101", 5: "10101", 6: "0101",
    7: "11101", 8: "00011", 9: "10011", 10: "01011", 11: "11011", 12: "00111",
}

HOUR_CODES = {
    0: "00011", 1: "10011", 2: "01011", 3: "11011", 4: "00111", 5: "10111",
    6: "01111", 7: "11111", 8: "00001", 9: "10001", 10: "01001", 11: "11001",
    12: "00101", 13: "10101", 14: "01101", 15: "11101", 16: "00010", 17: "10010",
    18: "01010", 19: "11010", 20: "00110", 21: "10110", 22: "01110", 23: "11110",
}

# 10年周期で繰り返す年符号 (昭和60年/1985年基準)
YEAR_PATTERNS = [
    "10101", "01101", "11101", "00011", "10011",
    "01011", "10001", "01001", "11001", "00101",
]


def get_year_code(year: int) -> str:
    """西暦から10年周期の年符号(5bit)を取得"""
    idx = (year - 1985) % 10
    return YEAR_PATTERNS[idx]


def generate_ews_bitstring(region_key: str, year: int, month: int, day: int,
                            hour: int, blocks: int = 6) -> str:
    """
    第二種開始信号（津波警報等）の送信ビット列を組み立てる。
    region_key が REGION_CODES に存在しない場合は「全国共通」にフォールバック
    する（指定ミスで無音・例外になるより、全国共通で確実に鳴らす方を優先）。
    """
    region_code = REGION_CODES.get(region_key, REGION_CODES["全国共通"])

    year_code = get_year_code(year)
    month_code = MONTH_CODES.get(month, "10001")
    day_code = DAY_CODES.get(day, "10000")
    hour_code = HOUR_CODES.get(hour, "00011")

    # プリアンブル (前置符号 16bit)
    preamble = "1100110011001100"
    # スタートコード（開始符号 8bit）: 第二種 (津波警報等)
    start_code = "10010101"

    # 1ブロック (58ビット) の構成
    block_bits = (
        preamble +
        start_code +
        region_code +
        day_code + month_code + "0" +    # 月日区分符号 (11bit)
        year_code + hour_code + "0"      # 年時区分符号 (11bit)
    )
    return block_bits * max(1, blocks)


def synthesize_ews_audio(bitstring: str, pre_tone_sec: float = 0.2,
                          post_tone_sec: float = 0.2) -> bytes:
    """位相連続AFSK PCM音声データ (16bit Mono PCM, リトルエンディアン) を生成する。"""
    pcm_data = bytearray()
    phase = 0.0
    two_pi = 2.0 * math.pi
    max_amplitude = 28000  # クリッピング防止用振幅上限

    # 1. 前置固定音 (1024 Hz 単一正弦波)
    if pre_tone_sec > 0:
        num_samples_pre = int(SAMPLE_RATE * pre_tone_sec)
        phase_step = two_pi * FREQ_MARK / SAMPLE_RATE
        for _ in range(num_samples_pre):
            sample_val = int(max_amplitude * math.sin(phase))
            pcm_data.extend(struct.pack('<h', sample_val))
            phase += phase_step
            if phase >= two_pi:
                phase -= two_pi

    # 2. AFSKデータ信号部 (1=1024Hz, 0=640Hz)
    for bit in bitstring:
        freq = FREQ_MARK if bit == '1' else FREQ_SPACE
        phase_step = two_pi * freq / SAMPLE_RATE
        for _ in range(SAMPLES_PER_BIT):
            sample_val = int(max_amplitude * math.sin(phase))
            pcm_data.extend(struct.pack('<h', sample_val))
            phase += phase_step
            if phase >= two_pi:
                phase -= two_pi

    # 3. 後置固定音 (1024 Hz 単一正弦波)
    if post_tone_sec > 0:
        num_samples_post = int(SAMPLE_RATE * post_tone_sec)
        phase_step = two_pi * FREQ_MARK / SAMPLE_RATE
        for _ in range(num_samples_post):
            sample_val = int(max_amplitude * math.sin(phase))
            pcm_data.extend(struct.pack('<h', sample_val))
            phase += phase_step
            if phase >= two_pi:
                phase -= two_pi

    return bytes(pcm_data)


def generate_ews_pcm(region_key: str = "全国共通", blocks: int = 6,
                      pre_tone_sec: float = 0.2, post_tone_sec: float = 0.2,
                      when: datetime.datetime | None = None) -> bytes:
    """
    現在時刻（または指定時刻）を元に、第二種開始信号（津波警報等）の
    EWS信号音をPCMバイト列として生成する。外部ファイルへは一切出力しない。

    Parameters
    ----------
    region_key : REGION_CODES のキー（例: "全国共通", "熊本県"）
    blocks     : 送信ブロック数（1ブロック=58bit、約0.9秒）
    pre_tone_sec / post_tone_sec : 前置・後置固定音（1024Hz）の長さ（秒）
    when       : 信号に埋め込む日時。省略時は現在時刻を使用

    Returns
    -------
    16bit / モノラル / 44100Hz の生PCMバイト列
    """
    dt = when or datetime.datetime.now()
    bitstring = generate_ews_bitstring(
        region_key, dt.year, dt.month, dt.day, dt.hour, blocks
    )
    return synthesize_ews_audio(
        bitstring, pre_tone_sec=pre_tone_sec, post_tone_sec=post_tone_sec
    )
