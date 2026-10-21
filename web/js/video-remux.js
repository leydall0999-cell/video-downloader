/**
 * video-remux.js —— 浏览器内「双轨 MP4 合并」（不重编码，零服务端算力）
 *
 * 背景（2026-10-21）：抖音 PC 网页给的是 DASH 分离轨——
 *   media-video-avc1（h264 纯视频，ffprobe 实测无音轨）+ media-audio（aac 纯音频）。
 *   网页版要「零成本直连保存」，就必须在浏览器里把两轨合成一个 mp4，
 *   否则用户拿到的会是一个无声视频。
 *
 * 做法：mp4box.js 只做 demux（读出编码后的样本，不解码），mp4-muxer 只做 remux
 * （重新封装），编码块原样直通 ⇒ **画质无损、CPU 极低**，不需要 ffmpeg.wasm。
 *
 * 依赖（由 app.js 懒加载，均本地自托管）：
 *   js/vendor_mp4box.min.js   → window.MP4Box.createFile()
 *   js/vendor_mp4muxer.min.js → window.Mp4Muxer.{Muxer, ArrayBufferTarget}
 */
(function () {
  'use strict';

  // AAC 采样率索引表（ISO 14496-3），仅在拿不到原始 ASC 时才用来兜底拼 LC 配置。
  var AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
    16000, 12000, 11025, 8000, 7350];

  function aacFreqIndex(rate) {
    for (var i = 0; i < AAC_RATES.length; i++) if (AAC_RATES[i] === rate) return i;
    return 4; // 兜底 44100
  }

  /** 兜底用：2 字节 AudioSpecificConfig（AAC-LC）。真机抖音是 HE-AACv2，走下面的 esds 解析。 */
  function aacAudioSpecificConfig(rate, channels) {
    var objType = 2;                        // AAC-LC
    var idx = aacFreqIndex(rate);
    var val = (objType << 11) | (idx << 7) | ((channels || 2) << 3);
    return new Uint8Array([(val >> 8) & 0xff, val & 0xff]);
  }

  /** 读一个 MPEG-4 描述符 TLV（tag 1 字节 + 最长 4 字节变长长度）。 */
  function readDesc(b, p) {
    var tag = b[p++], len = 0, c, k;
    for (k = 0; k < 4; k++) {
      c = b[p++];
      len = (len << 7) | (c & 0x7f);
      if (!(c & 0x80)) break;
    }
    return { tag: tag, len: len, start: p };
  }

  /**
   * 从音频文件的 esds 里取**原始 AudioSpecificConfig 字节**。
   * 抖音音频是 HE-AACv2（ASC=eb8a0800，AOT=29），自己按 AAC-LC 拼会解码报
   * 「too many QMF subbands / SBR reset failed」⇒ 必须原样搬运，不能重算。
   */
  function aacConfigFromEsds(bytes) {
    var i, size, body, d1, d2, d3, flags, p, asc;
    for (i = 4; i < bytes.length - 8; i++) {
      // 'esds'
      if (bytes[i] !== 0x65 || bytes[i + 1] !== 0x73 || bytes[i + 2] !== 0x64 || bytes[i + 3] !== 0x73) continue;
      size = ((bytes[i - 4] << 24) | (bytes[i - 3] << 16) | (bytes[i - 2] << 8) | bytes[i - 1]) >>> 0;
      if (!size || i - 4 + size > bytes.length) continue;
      body = bytes.subarray(i + 4, i - 4 + size);   // 跳过 version/flags 前的 4 字节 FullBox 头
      try {
        p = 4;                                       // FullBox version+flags
        d1 = readDesc(body, p);                      // 0x03 ES_Descriptor
        if (d1.tag !== 0x03) continue;
        p = d1.start + 2;                            // ES_ID
        flags = body[p++];
        if (flags & 0x80) p += 2;                    // dependsOn_ES_ID
        if (flags & 0x40) p += 1 + body[p];          // URL
        if (flags & 0x20) p += 2;                    // OCR_ES_ID
        d2 = readDesc(body, p);                      // 0x04 DecoderConfigDescriptor
        if (d2.tag !== 0x04) continue;
        p = d2.start + 13;                           // objectType+streamType+bufferSize+max/avgBitrate
        d3 = readDesc(body, p);                      // 0x05 DecoderSpecificInfo
        if (d3.tag !== 0x05 || !d3.len || d3.start + d3.len > body.length) continue;
        asc = body.subarray(d3.start, d3.start + d3.len);
        return new Uint8Array(asc);
      } catch (_) { continue; }
    }
    return null;
  }

  /** mp4box 把 SPS/PPS 存成 { length, nalu } 包装对象（不是裸 Uint8Array），必须取 .nalu。 */
  function naluBytes(x) {
    if (!x) return new Uint8Array(0);
    if (x instanceof Uint8Array) return x;
    if (x.nalu instanceof Uint8Array) return x.nalu;
    if (x.data instanceof Uint8Array) return x.data;
    return new Uint8Array(0);
  }

  /** 从 avc1 样本描述里手拼 avcC（ISO 14496-15 §5.3.3.1），供解码器当 description。 */
  function avcDecoderConfig(avcC) {
    // high profile 的尾部扩展（chroma_format_idc 等）要原样带上，否则部分解码器认不出
    var ext = avcC.ext instanceof Uint8Array ? avcC.ext : new Uint8Array(0);
    var sps = [].concat((avcC.SPS || []).map(naluBytes)).filter(function (b) { return b.length; });
    var pps = [].concat((avcC.PPS || []).map(naluBytes)).filter(function (b) { return b.length; });
    var len = 7 + ext.length;   // 5 头字节 + numSPS + numPPS + ext
    for (var i = 0; i < sps.length; i++) len += 2 + sps[i].length;
    for (var j = 0; j < pps.length; j++) len += 2 + pps[j].length;
    var out = new Uint8Array(len);
    var p = 0;
    out[p++] = 1;                                    // configurationVersion
    out[p++] = avcC.AVCProfileIndication || 0x64;
    out[p++] = avcC.profile_compatibility || 0;
    out[p++] = avcC.AVCLevelIndication || 0x1f;
    out[p++] = 0xfc | ((avcC.lengthSizeMinusOne === undefined ? 3 : avcC.lengthSizeMinusOne) & 0x03);
    out[p++] = 0xe0 | (sps.length & 0x1f);
    for (var k = 0; k < sps.length; k++) {
      out[p++] = (sps[k].length >> 8) & 0xff;
      out[p++] = sps[k].length & 0xff;
      out.set(sps[k], p); p += sps[k].length;
    }
    out[p++] = pps.length & 0xff;
    for (var m = 0; m < pps.length; m++) {
      out[p++] = (pps[m].length >> 8) & 0xff;
      out[p++] = pps[m].length & 0xff;
      out.set(pps[m], p); p += pps[m].length;
    }
    if (ext.length) { out.set(ext, p); p += ext.length; }
    return out.subarray(0, p);
  }

  /** 用 mp4box 解出一条轨的全部样本（只 demux，不解码）。 */
  function demuxTrack(bytes, want) {
    return new Promise(function (resolve, reject) {
      if (typeof window.MP4Box === 'undefined') return reject(new Error('mp4box 未加载'));
      var file = window.MP4Box.createFile();
      var samples = [];
      var trackInfo = null;
      var entry = null;
      var settled = false;

      function finish() {
        if (settled) return;
        settled = true;
        if (!trackInfo) return reject(new Error('没找到 ' + want + ' 轨'));
        if (!samples.length) return reject(new Error(want + ' 轨没有样本'));
        resolve({ track: trackInfo, entry: entry, samples: samples });
      }

      file.onError = function (e) { if (!settled) { settled = true; reject(new Error('mp4box: ' + e)); } };

      file.onReady = function (info) {
        var list = want === 'video' ? info.videoTracks : info.audioTracks;
        if (!list || !list.length) return finish();
        trackInfo = list[0];
        try {
          entry = file.getTrackById(trackInfo.id).mdia.minf.stbl.stsd.entries[0];
        } catch (_) { entry = null; }
        file.setExtractionOptions(trackInfo.id, null, { nbSamples: 1000 });
        file.onSamples = function (id, user, batch) {
          for (var i = 0; i < batch.length; i++) samples.push(batch[i]);
        };
        file.start();
      };

      try {
        var buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        buf.fileStart = 0;
        file.appendBuffer(buf);
        file.flush();
      } catch (err) {
        if (!settled) { settled = true; reject(err); }
        return;
      }
      // flush 之后样本已同步回调完毕
      finish();
    });
  }

  /** 双轨 mp4 合并入口。返回 { blob, width, height, durationUs }。 */
  async function remux(videoBytes, audioBytes) {
    if (typeof window.Mp4Muxer === 'undefined') throw new Error('mp4-muxer 未加载');

    var v = await demuxTrack(videoBytes, 'video');
    var a = await demuxTrack(audioBytes, 'audio');

    var vEntry = v.entry;
    var avcC = vEntry && vEntry.avcC;
    if (!avcC) throw new Error('视频轨缺少 avcC（非 h264？）');
    var vDesc = avcDecoderConfig(avcC);
    var width = v.track.track_width || v.track.video && v.track.video.width || 0;
    var height = v.track.track_height || v.track.video && v.track.video.height || 0;

    // 音频参数：优先样本描述里的显式值，缺了按 ASC 兜底
    var aEntry = a.entry;
    var sampleRate = (aEntry && aEntry.samplerate) || a.track.audio && a.track.audio.sample_rate || 44100;
    var channels = (aEntry && aEntry.channel_count) || a.track.audio && a.track.audio.channel_count || 2;
    // 🔴 原始 ASC 必须原样搬运（抖音是 HE-AACv2 / AOT=29），拿不到才退回自拼的 AAC-LC
    var aDesc = aacConfigFromEsds(audioBytes) || aacAudioSpecificConfig(sampleRate, channels);

    var Muxer = window.Mp4Muxer.Muxer;
    var ArrayBufferTarget = window.Mp4Muxer.ArrayBufferTarget;
    var muxer = new Muxer({
      target: new ArrayBufferTarget(),
      video: { codec: 'avc', width: width, height: height, frameRate: 30 },
      audio: { codec: 'aac', numberOfChannels: channels, sampleRate: sampleRate },
      fastStart: 'in-memory',
      firstTimestampBehavior: 'offset',
    });

    var vMeta = { decoderConfig: { codec: 'avc1.' + [avcC.AVCProfileIndication, avcC.profile_compatibility, avcC.AVCLevelIndication].map(function (n) { return ('0' + (n || 0).toString(16)).slice(-2); }).join(''), description: vDesc } };
    var aMeta = { decoderConfig: { codec: 'mp4a.40.2', description: aDesc } };

    var vTimescale = v.track.timescale || 1000;
    var aTimescale = a.track.timescale || 1000;
    var i, s, us, dus, off;

    // 源文件靠 edit list 把「首帧 cts=1024（15360 时基 → 66.7ms）」映射成 0；
    // 我们内联两条轨没有 edit list，必须手动把最小 cts 平移到 0，否则视频比音频晚 66ms。
    var vShift = Infinity;
    for (i = 0; i < v.samples.length; i++) if (v.samples[i].cts < vShift) vShift = v.samples[i].cts;
    if (!isFinite(vShift)) vShift = 0;

    // 用 addVideoChunkRaw，第 6 个参数 compositionTimeOffset = cts - dts 才吃得下 B 帧：
    // 抖音 h264 有 B 帧，cts 不单调，直接喂 cts 会触发
    // 「Timestamps must be monotonically increasing」；给出 ctts 偏移后
    // muxer 内部 decodeTimestamp = timestamp - offset 恰好还原 DTS，既单调又保留帧显示顺序。
    // 走 raw 接口也顺带免掉对 WebCodecs（EncodedVideoChunk）的依赖。
    for (i = 0; i < v.samples.length; i++) {
      s = v.samples[i];
      us = Math.round(((s.cts - vShift) * 1e6) / vTimescale);
      dus = Math.round((s.duration * 1e6) / vTimescale);
      off = Math.round(((s.cts - vShift - s.dts) * 1e6) / vTimescale);
      muxer.addVideoChunkRaw(s.data, s.is_sync ? 'key' : 'delta', us, dus, i === 0 ? vMeta : undefined, off);
    }
    for (i = 0; i < a.samples.length; i++) {
      s = a.samples[i];
      us = Math.round((s.cts * 1e6) / aTimescale);
      dus = Math.round((s.duration * 1e6) / aTimescale);
      muxer.addAudioChunkRaw(s.data, 'key', us, dus, i === 0 ? aMeta : undefined);
    }

    muxer.finalize();
    var buf = muxer.target.buffer;
    var lastV = v.samples[v.samples.length - 1];
    var durationUs = Math.round(((lastV.cts + lastV.duration) * 1e6) / vTimescale);
    return {
      blob: new Blob([buf], { type: 'video/mp4' }),
      width: width,
      height: height,
      durationUs: durationUs,
      videoSamples: v.samples.length,
      audioSamples: a.samples.length,
    };
  }

  window.VDLRemux = {
    ready: function () {
      return typeof window.MP4Box !== 'undefined' && typeof window.Mp4Muxer !== 'undefined';
    },
    remux: remux,
  };
})();
