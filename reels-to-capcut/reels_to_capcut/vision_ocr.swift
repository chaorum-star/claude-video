import CoreGraphics
import Foundation
import ImageIO
import Vision

struct Response: Codable {
    let lines: [String]?
    let error: String?
}

func emit(_ response: Response, code: Int32 = 0) -> Never {
    let data = try! JSONEncoder().encode(response)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(code)
}

guard CommandLine.arguments.count > 1 else {
    emit(Response(lines: nil, error: "image path required"), code: 2)
}

let path = CommandLine.arguments[1]
guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    emit(Response(lines: nil, error: "cannot read image"), code: 3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["ko-KR", "en-US"]
request.usesLanguageCorrection = true

do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let ordered = (request.results ?? []).sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.03 {
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }
    let lines = ordered.compactMap { $0.topCandidates(1).first?.string }
    emit(Response(lines: lines, error: nil))
} catch {
    emit(Response(lines: nil, error: error.localizedDescription), code: 4)
}
