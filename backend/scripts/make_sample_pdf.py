"""生成演示用样本教材 PDF（P0-03 / P0-05 的测试基准）。

为什么必须自带样本：PRD Q5 明确「内置自制公有领域/改写版样本，避免版权风险」。
真实 KSSM 课本有版权，不能进仓库；而这个脚本产出的 PDF 结构（5 章 / 15 节 / 45 小节、
三级层级、≥100 页）与真实教材同构，才能让「目录树三级识别」「Top-5 同章」这类断言有意义。

用法：
    backend/.venv/Scripts/python.exe backend/scripts/make_sample_pdf.py
    backend/.venv/Scripts/python.exe backend/scripts/make_sample_pdf.py --out backend/data/sample.pdf
    backend/.venv/Scripts/python.exe backend/scripts/make_sample_pdf.py --chapters 2   # 快速小样本

确定性：全文内容由常量表 + 索引推导，不含随机数；同一参数两次生成页数完全一致。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
MARGIN_X = 56.0
MARGIN_Y = 72.0
BODY_FONT_SIZE = 10.5
HEADING_FONT_SIZE = 13.0
LINE_HEIGHT_RATIO = 1.7
# 正文换行宽度（pt）。留出右边距，避免中文行溢出页面
TEXT_WIDTH = PAGE_WIDTH - MARGIN_X * 2
FONT_NAME = "china-s"  # PyMuPDF 内置 CJK 字体，无需外部字体文件


@dataclass(frozen=True)
class SubSpec:
    code: str
    title: str
    facts: tuple[str, ...]


@dataclass(frozen=True)
class SectionSpec:
    code: str
    title: str
    intro: str
    subs: tuple[SubSpec, ...]


@dataclass(frozen=True)
class ChapterSpec:
    number: int
    title: str
    # 章标题行必须能在一行内排下（全角字体下 TEXT_WIDTH/13pt ≈ 37 字），
    # 否则 insert_text 会自动折行，章节号与标题被拆开，解析器就认不出来了
    short_title: str
    intro: str
    sections: tuple[SectionSpec, ...]


# 每章一段通用叙述，用来把页面填到「有实质内容」的密度（每页 ≥ 400 字符）
_FILLER: dict[int, tuple[str, ...]] = {
    1: (
        "历史学训练的核心不是背年份，而是学会判断一则材料的可信度与立场。",
        "课堂讨论时先分清事实陈述与价值判断，再谈因果，答题才不会偏题。",
        "Sejarah 一词源自阿拉伯语 syajaratun，意为树，强调时间上的生长与延续。",
    ),
    2: (
        "早期王国依靠河流与季风维持贸易网络，港口城市因此成为权力中心。",
        "农业与海洋贸易并不是对立的两条线，同一个政权往往两者兼营。",
        "考古发现中的陶片、碑铭与钱币，是复原这些政权形态的主要依据。",
    ),
    3: (
        "西方势力的到来与工业革命后的原料需求、航线安全直接相关。",
        "条约的签订过程往往伴随武力威慑，条款文本与实际控制范围需要分别评估。",
        "地方统治者的应对策略在妥协、抵抗与谈判之间不断调整。",
    ),
    4: (
        "讨论 Malayan Union 时必须区分「英国的政策意图」与「当地社会的反应」两条线。",
        "宪法文本里的每一处措辞变化，背后都是各方力量对比的结果。",
        "考试作答时先给制度安排，再给社会反应，最后给影响，层次分明才拿高分。",
    ),
    5: (
        "马来西亚的成立是冷战格局、区域安全与本地政治协商共同作用的结果。",
        "1963 年是一个时间节点，但制度磨合一直延续到六十年代末。",
        "比较不同地区加入联邦的条件，能看清联邦制的弹性与代价。",
    ),
}


CHAPTERS: tuple[ChapterSpec, ...] = (
    ChapterSpec(
        number=1,
        short_title="Warisan Sejarah",
        title="Warisan Sejarah Malaysia",
        intro="本章建立历史学的基本概念与研究方法，为后续章节提供分析工具。",
        sections=(
            SectionSpec(
                code="1.1",
                title="Pengertian Sejarah",
                intro="先厘清「历史」这个词在学术语境中的确切含义。",
                subs=(
                    SubSpec(
                        code="1.1.1",
                        title="Maksud Sejarah",
                        facts=(
                            "Sejarah 指对人类过去活动的系统记录与研究，强调证据与解释。",
                            "历史不是简单的时间清单，而是对因果关系的重建。",
                            "同一事件在不同史料中的叙述差异，本身就是研究对象。",
                        ),
                    ),
                    SubSpec(
                        code="1.1.2",
                        title="Ciri-ciri Sejarah",
                        facts=(
                            "历史具备时间性、事实性与解释性三个基本特征。",
                            "事实性要求结论可被史料复核，解释性要求说明因果关系。",
                            "缺少出处的叙述只能算传说，不能作为考试依据。",
                        ),
                    ),
                    SubSpec(
                        code="1.1.3",
                        title="Kepentingan Sejarah",
                        facts=(
                            "学习历史有助于理解制度与身份的由来，培养判断力。",
                            "历史教育强调多元视角，避免单一叙事。",
                            "在考试中，引用具体史料比空泛评论更容易得分。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="1.2",
                title="Sumber Sejarah",
                intro="史料是历史研究的原料，按来源可分为第一手与第二手。",
                subs=(
                    SubSpec(
                        code="1.2.1",
                        title="Sumber Primer",
                        facts=(
                            "第一手史料指与事件同时代产生的材料，如公文、日记、碑铭。",
                            "第一手材料也可能带有立场偏差，需要交叉验证。",
                            "档案编号与收藏地必须记录，方便复核。",
                        ),
                    ),
                    SubSpec(
                        code="1.2.2",
                        title="Sumber Sekunder",
                        facts=(
                            "第二手史料是后人依据第一手材料写成的研究与教科书。",
                            "教科书的叙述经过筛选，回答时要注意它的概括层次。",
                            "引用第二手材料时应说明作者与出版年份。",
                        ),
                    ),
                    SubSpec(
                        code="1.2.3",
                        title="Kaedah Penyelidikan",
                        facts=(
                            "研究方法包括文献分析、口述访谈与实地考察。",
                            "口述史料能补足官方档案的空白，但需要谨慎处理记忆误差。",
                            "不同方法得到的结论相互印证，可信度才高。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="1.3",
                title="Kaedah Pengkajian",
                intro="本节介绍三种常用研究方法及其适用边界。",
                subs=(
                    SubSpec(
                        code="1.3.1",
                        title="Kaedah Lisan",
                        facts=(
                            "口述方法适合研究缺乏文字记录的社会群体。",
                            "访谈需要设计问题清单，并记录时间、地点与受访者背景。",
                            "口述材料的价值在于细节与情感，局限在于可复核性。",
                        ),
                    ),
                    SubSpec(
                        code="1.3.2",
                        title="Kaedah Bertulis",
                        facts=(
                            "文献方法依赖档案、报刊与私人文书。",
                            "阅读文献时要区分原始记载与转述，避免二手转引。",
                            "文本的语言与用词变化，往往反映时代观念的变迁。",
                        ),
                    ),
                    SubSpec(
                        code="1.3.3",
                        title="Kaedah Arkeologi",
                        facts=(
                            "考古方法通过发掘地层与遗物推断年代与生活方式。",
                            "碳十四测年与陶器类型学是常用的断代手段。",
                            "遗址的整体布局比单件器物更能说明社会结构。",
                        ),
                    ),
                ),
            ),
        ),
    ),
    ChapterSpec(
        number=2,
        short_title="Kerajaan Awal",
        title="Kerajaan Awal di Asia Tenggara",
        intro="本章讨论东南亚早期政权的经济基础与政治形态。",
        sections=(
            SectionSpec(
                code="2.1",
                title="Kerajaan Agraria",
                intro="农业型政权以稻作为核心，人口与水利是其权力基础。",
                subs=(
                    SubSpec(
                        code="2.1.1",
                        title="Ekonomi Padi",
                        facts=(
                            "稻作需要稳定的水源与灌溉网络，因而形成集中的管理体制。",
                            "Angkor 的水利系统规模庞大，反映王权对劳动力的动员能力。",
                            "农业剩余支撑了宗教建筑与常备官僚的维持。",
                        ),
                    ),
                    SubSpec(
                        code="2.1.2",
                        title="Struktur Sosial",
                        facts=(
                            "农业社会的等级结构通常由土地与劳役关系决定。",
                            "寺庙既是宗教中心，也是仓储与行政节点。",
                            "等级流动有限，但专业技能群体享有一定地位。",
                        ),
                    ),
                    SubSpec(
                        code="2.1.3",
                        title="Kepercayaan",
                        facts=(
                            "早期信仰融合了本地精灵崇拜与外来宗教体系。",
                            "王权常借助宗教符号强化合法性。",
                            "宗教建筑的空间布局体现宇宙观。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="2.2",
                title="Kerajaan Maritim",
                intro="海洋型政权依靠季风贸易与港口管理获取财富。",
                subs=(
                    SubSpec(
                        code="2.2.1",
                        title="Jaringan Perdagangan",
                        facts=(
                            "季风决定了航行季节，港口因此成为固定的交易节点。",
                            "Srivijaya 控制海峡通道，从转口贸易中抽取收益。",
                            "贸易网络把中国、印度与群岛市场连接起来。",
                        ),
                    ),
                    SubSpec(
                        code="2.2.2",
                        title="Pelabuhan dan Cukai",
                        facts=(
                            "港口政权通过关税、仓储与护航服务维持收入。",
                            "港口治理能力决定了商人是否愿意长期停靠。",
                            "货币与度量标准的统一降低了交易成本。",
                        ),
                    ),
                    SubSpec(
                        code="2.2.3",
                        title="Ketenteraan Laut",
                        facts=(
                            "海上力量用于护航与威慑，而非占领远地领土。",
                            "舰队规模受制于财政与人力，通常季节性集结。",
                            "安全承诺是港口吸引贸易的重要条件。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="2.3",
                title="Perkembangan Budaya",
                intro="贸易带来的人员流动促成了语言、宗教与技术的交融。",
                subs=(
                    SubSpec(
                        code="2.3.1",
                        title="Bahasa dan Tulisan",
                        facts=(
                            "梵文与阿拉伯文先后影响本地书写系统。",
                            "碑铭是研究早期语言接触的重要材料。",
                            "文字的使用与宗教传播密切相关。",
                        ),
                    ),
                    SubSpec(
                        code="2.3.2",
                        title="Seni Bina",
                        facts=(
                            "宗教建筑的材料与工艺反映了技术交流的路径。",
                            "石构与木构传统的差异与当地资源条件有关。",
                            "建筑装饰中的纹样常具有象征意义。",
                        ),
                    ),
                    SubSpec(
                        code="2.3.3",
                        title="Ilmu dan Teknologi",
                        facts=(
                            "造船、冶金与纺织技术随贸易路线传播。",
                            "技术引进往往伴随工匠群体的迁移。",
                            "本地对技术的改造，体现了环境适应能力。",
                        ),
                    ),
                ),
            ),
        ),
    ),
    ChapterSpec(
        number=3,
        short_title="Kedatangan Kuasa Barat",
        title="Kedatangan Kuasa Barat",
        intro="本章分析西方势力进入本区域的动因、方式与后果。",
        sections=(
            SectionSpec(
                code="3.1",
                title="Faktor Kedatangan",
                intro="动因可以分为经济、战略与政治三类。",
                subs=(
                    SubSpec(
                        code="3.1.1",
                        title="Faktor Ekonomi",
                        facts=(
                            "工业革命后对锡、橡胶等原料的需求迅速增长。",
                            "苏伊士运河通航缩短了航程，贸易量随之上升。",
                            "资本输出需要稳定的政治环境与法律保障。",
                        ),
                    ),
                    SubSpec(
                        code="3.1.2",
                        title="Faktor Strategik",
                        facts=(
                            "海峡航道的安全直接关系到帝国贸易命脉。",
                            "海军补给站与加煤站的选址具有战略意义。",
                            "区域势力平衡的考虑常压过单纯的商业计算。",
                        ),
                    ),
                    SubSpec(
                        code="3.1.3",
                        title="Faktor Politik",
                        facts=(
                            "本土政治动荡为外部干预提供了理由。",
                            "王位继承争议常被外部势力利用。",
                            "条约体系逐步把地方政权纳入外部法律框架。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="3.2",
                title="Kesan Kedatangan",
                intro="后果既包括制度重组，也包括社会结构的改变。",
                subs=(
                    SubSpec(
                        code="3.2.1",
                        title="Pentadbiran",
                        facts=(
                            "驻官制度取代了原有的朝贡关系。",
                            "税收与土地登记制度被重新设计。",
                            "行政语言与教育体系随之改变。",
                        ),
                    ),
                    SubSpec(
                        code="3.2.2",
                        title="Ekonomi",
                        facts=(
                            "种植园与矿业的发展依赖外来劳动力。",
                            "铁路与港口建设服务于原料出口。",
                            "经济结构趋向单一化，对外部市场依赖加深。",
                        ),
                    ),
                    SubSpec(
                        code="3.2.3",
                        title="Masyarakat",
                        facts=(
                            "人口流动改变了城镇的族群构成。",
                            "族群分工与经济角色被制度性地固化。",
                            "教育与宗教机构成为社会流动的新渠道。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="3.3",
                title="Reaksi Tempatan",
                intro="地方社会的回应在抵抗、谈判与调适之间摆动。",
                subs=(
                    SubSpec(
                        code="3.3.1",
                        title="Penentangan",
                        facts=(
                            "早期抵抗多以地方领袖为核心，规模有限。",
                            "武装抵抗常因武器与补给劣势而失败。",
                            "抵抗记忆后来成为民族叙事的重要资源。",
                        ),
                    ),
                    SubSpec(
                        code="3.3.2",
                        title="Rundingan",
                        facts=(
                            "谈判策略强调保留宗教与习俗的自主空间。",
                            "文书往来的措辞差异反映了力量对比。",
                            "妥协换取的制度空间，为后续政治运动留下基础。",
                        ),
                    ),
                    SubSpec(
                        code="3.3.3",
                        title="Penyesuaian",
                        facts=(
                            "部分精英通过新式教育进入行政体系。",
                            "报章与社团成为表达意见的新平台。",
                            "调适并不等于认同，而是一种生存策略。",
                        ),
                    ),
                ),
            ),
        ),
    ),
    ChapterSpec(
        number=4,
        short_title="Malayan Union",
        title="Malayan Union dan Persekutuan Tanah Melayu",
        intro="本章是全书重点：从 Malayan Union 到马来亚联合邦，再到 1957 年独立。",
        sections=(
            SectionSpec(
                code="4.1",
                title="Malayan Union",
                intro="Malayan Union 于 1946 年 4 月 1 日正式生效，是英国战后重建计划的核心。",
                subs=(
                    SubSpec(
                        code="4.1.1",
                        title="Latar Belakang Malayan Union",
                        facts=(
                            "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本。",
                            "1945 年 10 月，英国派遣特使到马来亚，以威迫方式促使各州苏丹签署新宪制协议。",
                            "Malayan Union 把各州与海峡殖民地合并为单一殖民地，由英国总督直接统治。",
                        ),
                    ),
                    SubSpec(
                        code="4.1.2",
                        title="Kuasa Sultan",
                        facts=(
                            "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。",
                            "苏丹不再拥有对州务的实质否决权，各州主权被移交给英国总督。",
                            "公民权采属地原则，宽松的归化条件使非马来人人口结构面临改变。",
                        ),
                    ),
                    SubSpec(
                        code="4.1.3",
                        title="Reaksi Masyarakat",
                        facts=(
                            "马来社会强烈反对 Malayan Union，认为它削弱苏丹地位并威胁马来人政治地位。",
                            "1946 年 3 月，全马马来人代表大会在吉隆坡召开，各地苏丹亦拒绝出席移交仪式。",
                            "反对运动促成巫统成立，并最终迫使英国以马来亚联合邦取代 Malayan Union。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="4.2",
                title="Persekutuan Tanah Melayu",
                intro="1948 年成立的马来亚联合邦恢复了苏丹的州权，同时收紧公民权。",
                subs=(
                    SubSpec(
                        code="4.2.1",
                        title="Penubuhan Persekutuan",
                        facts=(
                            "马来亚联合邦于 1948 年 2 月 1 日成立，由英国驻最高专员与各州苏丹共同治理。",
                            "联合邦恢复了苏丹在州务上的权力，Malayan Union 的中央集权被部分撤回。",
                            "公民权条件收紧，申请者需满足居住年限与语言要求。",
                        ),
                    ),
                    SubSpec(
                        code="4.2.2",
                        title="Perjanjian 1948",
                        facts=(
                            "1948 年协议由各州苏丹与英国签订，明确州权与联邦权的划分。",
                            "协议文本保留了英国在防务与外交上的主导地位。",
                            "苏丹在宗教与习俗事务上的权威得到正式确认。",
                        ),
                    ),
                    SubSpec(
                        code="4.2.3",
                        title="Kesan Persekutuan",
                        facts=(
                            "联合邦体制为后来的选举政治提供了制度框架。",
                            "公民权与族群关系的安排，成为长期政治议题。",
                            "苏丹制度在宪制中的地位被重新确立，影响延续至今。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="4.3",
                title="Jalan ke Arah Kemerdekaan",
                intro="从联合邦到独立，关键节点是 1955 年选举与 1956 年伦敦谈判。",
                subs=(
                    SubSpec(
                        code="4.3.1",
                        title="Pilihan Raya 1955",
                        facts=(
                            "1955 年首次联邦立法议会选举，联盟党赢得绝大多数席位。",
                            "选举结果使本地政党获得与英国谈判的正当性。",
                            "竞选纲领把独立时间表列为首要议题。",
                        ),
                    ),
                    SubSpec(
                        code="4.3.2",
                        title="Rundingan London",
                        facts=(
                            "1956 年伦敦谈判确定 1957 年 8 月 31 日独立。",
                            "谈判同时设立宪制委员会，起草独立后的宪法。",
                            "防务协定在独立后仍保留英国军事存在的安排。",
                        ),
                    ),
                    SubSpec(
                        code="4.3.3",
                        title="Kemerdekaan 1957",
                        facts=(
                            "1957 年 8 月 31 日，马来亚联合邦正式独立。",
                            "独立宪法确立君主立宪与联邦制的基本架构。",
                            "独立后的首要任务是整合族群关系与经济发展。",
                        ),
                    ),
                ),
            ),
        ),
    ),
    ChapterSpec(
        number=5,
        short_title="Pembentukan Malaysia",
        title="Pembentukan Malaysia",
        intro="本章讨论 1963 年马来西亚成立的过程与争议。",
        sections=(
            SectionSpec(
                code="5.1",
                title="Idea Malaysia",
                intro="合并构想旨在平衡区域人口结构并加快非殖民化。",
                subs=(
                    SubSpec(
                        code="5.1.1",
                        title="Latar Belakang Idea",
                        facts=(
                            "合并构想希望以更大的联邦体平衡族群比例。",
                            "英国希望借此完成区域内的权力交接。",
                            "新加坡的地位与自治安排是谈判难点。",
                        ),
                    ),
                    SubSpec(
                        code="5.1.2",
                        title="Perundingan",
                        facts=(
                            "1961 年起多方展开磋商，成立委员会调查民意。",
                            "调查报告成为谈判的基础文件。",
                            "各方在税收、教育与公民权上反复博弈。",
                        ),
                    ),
                    SubSpec(
                        code="5.1.3",
                        title="Perjanjian 1963",
                        facts=(
                            "1963 年协定确立联邦的组成与权力分配。",
                            "沙巴与砂拉越获得若干自主权保障。",
                            "协定的解释争议延续至今。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="5.2",
                title="Reaksi Serantau",
                intro="邻国与区域组织的态度影响了成立进程。",
                subs=(
                    SubSpec(
                        code="5.2.1",
                        title="Indonesia",
                        facts=(
                            "印尼对合并持反对立场，认为影响区域势力平衡。",
                            "对抗政策导致边境摩擦与外交紧张。",
                            "冲突在 1966 年后逐步缓和。",
                        ),
                    ),
                    SubSpec(
                        code="5.2.2",
                        title="Filipina",
                        facts=(
                            "菲律宾对沙巴主权提出争议。",
                            "争议通过区域组织框架进行磋商。",
                            "争议长期存在但未阻断区域合作。",
                        ),
                    ),
                    SubSpec(
                        code="5.2.3",
                        title="Peranan MAPHILINDO",
                        facts=(
                            "区域协商机制尝试调解三方分歧。",
                            "协商成果有限，但确立了对话习惯。",
                            "区域组织后来转向经济与安全合作。",
                        ),
                    ),
                ),
            ),
            SectionSpec(
                code="5.3",
                title="1963 dan Selepas",
                intro="成立后的制度磨合与外部压力同时存在。",
                subs=(
                    SubSpec(
                        code="5.3.1",
                        title="Penubuhan Malaysia",
                        facts=(
                            "1963 年 9 月 16 日马来西亚正式成立。",
                            "新加坡于 1965 年退出联邦。",
                            "联邦制在实践中的弹性不断被检验。",
                        ),
                    ),
                    SubSpec(
                        code="5.3.2",
                        title="Cabaran Awal",
                        facts=(
                            "新国家面临安全、财政与族群整合三重挑战。",
                            "发展政策以农业与基础设施为优先。",
                            "区域合作成为对外政策主轴。",
                        ),
                    ),
                    SubSpec(
                        code="5.3.3",
                        title="Warisan",
                        facts=(
                            "联邦制与君主立宪延续为政治制度的基本框架。",
                            "教育语言政策持续调整。",
                            "历史叙事在多元社会中不断被重新讨论。",
                        ),
                    ),
                ),
            ),
        ),
    ),
)


def _text_width(line: str, font_size: float = BODY_FONT_SIZE) -> float:
    """估算行宽。

    关键：PyMuPDF 内置的 `china-s` 是**全角字体**，拉丁字母与数字同样占 1 em 宽
    （不是通常的 0.55 em）。按 0.55 估宽会算少一半，导致标题被 `insert_text` 自动折行，
    解析时章节号与标题被拆到两行 —— 这是「章节识别率莫名下降」的隐蔽根因。
    """
    return len(line) * font_size


def _wrap(text: str, max_width: float = TEXT_WIDTH) -> list[str]:
    """按估算宽度硬换行。

    额外约束：换行后的行首**不能是数字**。否则正文里以年份开头的行（如 "1946 年……"）
    可能被章节正则误判成标题，导致页码区间整体错位。
    """
    lines: list[str] = []
    current: list[str] = []
    width = 0.0
    for char in text:
        char_width = BODY_FONT_SIZE
        if width + char_width > max_width and current:
            # 下一行会以数字开头 → 提前一个字符断行，把数字留在本行
            if char.isdigit() and len(current) > 2:
                lines.append("".join(current))
                current, width = [char], char_width
                continue
            lines.append("".join(current))
            current, width = [], 0.0
        current.append(char)
        width += char_width
    if current:
        lines.append("".join(current))
    return lines


def _assert_heading_fits(line: str) -> None:
    """标题必须能单行排下。超宽会被 PyMuPDF 自动折行，章节号与标题被拆开 → 识别失败。"""
    width = _text_width(line, HEADING_FONT_SIZE)
    if width > TEXT_WIDTH:
        raise ValueError(
            f"标题超宽（{width:.0f}pt > {TEXT_WIDTH:.0f}pt）：{line!r}，请缩短后再生成"
        )


def _write_page(document: object, lines: list[str], *, heading: bool = False) -> None:
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)  # type: ignore[attr-defined]
    y = MARGIN_Y
    for index, line in enumerate(lines):
        is_heading = heading and index == 0
        if is_heading:
            _assert_heading_fits(line)
        size = HEADING_FONT_SIZE if is_heading else BODY_FONT_SIZE
        page.insert_text((MARGIN_X, y), line, fontname=FONT_NAME, fontsize=size, color=(0, 0, 0))
        y += size * LINE_HEIGHT_RATIO


def _paragraphs(chapter: ChapterSpec, section: SectionSpec, sub: SubSpec, count: int) -> list[str]:
    """按索引确定性拼接段落（无随机数）：事实句 + 本章填充句。"""
    filler = _FILLER.get(chapter.number, ())
    paragraphs: list[str] = []
    for index in range(count):
        sentences: list[str] = []
        for offset in range(2):
            sentences.append(sub.facts[(index + offset) % len(sub.facts)])
        if filler:
            sentences.append(filler[(index + section.code.count(".")) % len(filler)])
        paragraphs.append("".join(sentences))
    return paragraphs


def build_sample_pdf(
    path: str | Path,
    *,
    chapters: int = 5,
    pages_per_subsection: int = 2,
) -> int:
    """生成样本 PDF，返回总页数。

    `pages_per_subsection=2` + 5 章时总页数 ≈ 113 页，满足「≥100 页」的验收要求；
    测试里可传 `chapters=1, pages_per_subsection=1` 得到几十页的小样本跑得快。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    document = pymupdf.open()
    selected = CHAPTERS[: max(1, min(chapters, len(CHAPTERS)))]

    # 封面
    _write_page(
        document,
        [
            "Sejarah Tingkatan 4",
            "Edu-Spatial 演示样本（公有领域改写版，非真实课本）",
            "本文件由 backend/scripts/make_sample_pdf.py 生成，仅用于功能演示与自动化测试。",
            "Struktur: 5 Bab / 15 Bahagian / 45 Subbahagian",
        ],
        heading=True,
    )

    # 目录页：首行必须是 contents 标记，解析器据此整页跳过（§7.1 R0）
    toc_lines = ["Isi Kandungan"]
    for chapter in selected:
        toc_lines.append(f"Chapter {chapter.number}  {chapter.title}")
        for section in chapter.sections:
            toc_lines.append(f"  {section.code}  {section.title}")
    _write_page(document, toc_lines, heading=True)

    # 前言页（长度足够，不会被误判为标题）
    _write_page(
        document,
        _wrap(
            "本样本用于演示「上传私有教材 → 生成目录树 → 检索 → 生成多模态产物 → 锚点追问」"
            "的完整链路。内容依据公开历史常识改写，不引用任何受版权保护的原文；"
            "章节编号与结构参照 KSSM 教材的常见组织方式，便于验证三级目录识别与页码回溯。"
        )
    )

    for chapter in selected:
        # 章首页：同时出现 "Chapter N" 与 "N 标题"，去重后只保留一条一级标题
        chapter_lines = [f"Chapter {chapter.number}: {chapter.short_title}"]
        chapter_lines.extend(_wrap(chapter.intro))
        _write_page(document, chapter_lines, heading=True)

        for section in chapter.sections:
            section_lines = _wrap(f"{section.code} {section.title}")
            section_lines.extend(_wrap(section.intro))
            _write_page(document, section_lines, heading=True)

            for sub in section.subs:
                # 每页两段：一段事实、一段事实+本章语境，保证单页字符数足够撑起 chunk 密度
                paragraphs = _paragraphs(
                    chapter, section, sub, count=max(2, pages_per_subsection * 2)
                )
                for page_index in range(pages_per_subsection):
                    lines: list[str] = []
                    if page_index == 0:
                        # 小节标题只出现在该小节第一页，避免重复标题干扰区间计算
                        lines.append(f"{sub.code} {sub.title}")
                    for paragraph in paragraphs[page_index * 2 : page_index * 2 + 2]:
                        lines.extend(_wrap(paragraph))
                    _write_page(document, lines, heading=page_index == 0)

    document.save(str(target))
    page_count = document.page_count
    document.close()
    return page_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成演示用样本教材 PDF")
    default_out = Path(__file__).resolve().parents[1] / "data" / "sample_textbook.pdf"
    parser.add_argument("--out", default=str(default_out), help="输出路径")
    parser.add_argument("--chapters", type=int, default=5, help="生成前 N 章（1-5）")
    parser.add_argument("--pages-per-subsection", type=int, default=2, help="每个小节的页数")
    args = parser.parse_args(argv)

    page_count = build_sample_pdf(
        args.out, chapters=args.chapters, pages_per_subsection=args.pages_per_subsection
    )
    print(f"已生成 {args.out}（{page_count} 页）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
